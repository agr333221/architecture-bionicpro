"""bionicpro-auth: BFF (Backend for Frontend) между фронтендом и Keycloak.

- Реализует Authorization Code Flow + PKCE (S256) как конфиденциальный клиент.
- Токены Keycloak никогда не попадают во фронтенд: браузер получает только
  сессионную cookie (HttpOnly, Secure, SameSite=Lax).
- access_token хранится в Redis, refresh_token - в Redis в зашифрованном виде (AES-256-GCM).
- При истечении access_token сервис сам обновляет его через refresh_token.
- При каждом запросе к защищенному ресурсу session id ротируется (защита от session fixation).
- Запросы фронтенда к /api/* проксируются в reports-api с подстановкой access_token.
"""
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from . import keycloak, sessions
from .config import settings
from .pkce import code_challenge_s256, generate_code_verifier
from .sessions import Session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bionicpro-auth")

app = FastAPI(title="bionicpro-auth", version="1.0.0")

LOGIN_STATE_COOKIE = "bionicpro_login_state"
ALLOWED_IDP_HINTS = {"yandex"}


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------
def _set_session_cookie(response: Response, sid: str) -> None:
    response.set_cookie(
        key=settings.cookie_name,
        value=sid,
        max_age=settings.session_idle_ttl,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.cookie_name, path="/", httponly=True, secure=settings.cookie_secure, samesite="lax"
    )


def _safe_return_to(value: str | None) -> str:
    # Разрешаем только относительные пути, чтобы не получить open redirect
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


# ---------------------------------------------------------------------------
# Login: Authorization Code + PKCE
# ---------------------------------------------------------------------------
@app.get("/auth/login")
async def login(return_to: str | None = None, idp: str | None = None) -> Response:
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = generate_code_verifier()

    await sessions.save_login_state(
        state,
        {"code_verifier": code_verifier, "nonce": nonce, "return_to": _safe_return_to(return_to)},
    )

    params = {
        "client_id": settings.client_id,
        "response_type": "code",
        "redirect_uri": settings.redirect_uri,
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge_s256(code_verifier),
        "code_challenge_method": "S256",
    }
    if idp in ALLOWED_IDP_HINTS:
        params["kc_idp_hint"] = idp

    response = RedirectResponse(f"{settings.authorize_endpoint}?{urlencode(params)}", status_code=302)
    # state дополнительно привязываем к браузеру, чтобы исключить login CSRF
    response.set_cookie(
        LOGIN_STATE_COOKIE, state, max_age=600, path="/auth",
        httponly=True, secure=settings.cookie_secure, samesite="lax",
    )
    return response


@app.get("/auth/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None,
                   error: str | None = None, error_description: str | None = None) -> Response:
    if error:
        log.warning("login error from keycloak: %s %s", error, error_description)
        return RedirectResponse(f"/?{urlencode({'login_error': error})}", status_code=302)
    if not code or not state:
        raise HTTPException(400, "code and state are required")

    browser_state = request.cookies.get(LOGIN_STATE_COOKIE)
    if not browser_state or not secrets.compare_digest(browser_state, state):
        raise HTTPException(400, "state mismatch")

    login_data = await sessions.pop_login_state(state)
    if login_data is None:
        raise HTTPException(400, "login state expired, start again")

    try:
        tokens = await keycloak.exchange_code(code, login_data["code_verifier"])
        session = keycloak.session_from_tokens(tokens, nonce=login_data["nonce"])
    except Exception as exc:  # noqa: BLE001
        log.exception("token exchange failed: %r", exc)
        raise HTTPException(401, "authentication failed") from exc

    # Новая сессия всегда получает новый случайный id (никогда не берем id от клиента)
    sid = sessions.new_session_id()
    await sessions.save(sid, session)
    log.info("login ok: user=%s roles=%s", session.username, session.roles)

    response = RedirectResponse(login_data["return_to"], status_code=302)
    _set_session_cookie(response, sid)
    response.delete_cookie(LOGIN_STATE_COOKIE, path="/auth")
    return response


# ---------------------------------------------------------------------------
# Проверка сессии, обновление access_token и ротация session id
# ---------------------------------------------------------------------------
class SessionContext:
    def __init__(self, sid: str, session: Session):
        self.sid = sid
        self.session = session


async def _require_session(request: Request) -> SessionContext:
    sid = request.cookies.get(settings.cookie_name)
    if not sid:
        raise HTTPException(401, "not authenticated")

    loaded = await sessions.load(sid)
    if loaded is None:
        raise HTTPException(401, "session expired")
    current_sid, session, via_grace = loaded

    # access_token истек или скоро истечет -> обновляем через refresh_token
    if session.access_expires_at - time.time() < settings.access_token_skew:
        try:
            started = time.monotonic()
            tokens = await keycloak.refresh(session.refresh_token)
            got_tokens = time.monotonic()
            keycloak.apply_refreshed_tokens(session, tokens)
            log.info("access_token refreshed for user=%s (keycloak %.2fs, verify %.2fs)", session.username,
                     got_tokens - started, time.monotonic() - got_tokens)
        except httpx.HTTPError as exc:
            # Временная недоступность Keycloak: сессию не теряем, клиент может повторить запрос
            log.warning("keycloak unavailable during refresh for user=%s: %r", session.username, exc)
            raise HTTPException(503, "identity provider is temporarily unavailable") from exc
        except Exception as exc:  # noqa: BLE001
            # refresh_token отозван или истек (invalid_grant) -> сессия завершена
            log.warning("refresh failed for user=%s: %r", session.username, exc)
            await sessions.delete(current_sid)
            raise HTTPException(401, "session expired") from exc

    if via_grace:
        # Параллельный запрос со старым id: сессия уже ротирована другим запросом,
        # повторно не ротируем, а просто отдаем актуальный id
        await sessions.save(current_sid, session)
        new_sid = current_sid
    else:
        new_sid = await sessions.rotate(current_sid, session)

    return SessionContext(new_sid, session)


def _with_rotated_cookie(response: Response, ctx: SessionContext) -> Response:
    _set_session_cookie(response, ctx.sid)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/auth/me")
async def me(request: Request) -> Response:
    ctx = await _require_session(request)
    return _with_rotated_cookie(JSONResponse(ctx.session.public_profile()), ctx)


@app.post("/auth/logout")
async def logout(request: Request) -> Response:
    sid = request.cookies.get(settings.cookie_name)
    if sid:
        loaded = await sessions.load(sid)
        if loaded:
            current_sid, session, _ = loaded
            try:
                await keycloak.logout(session.refresh_token)
            except Exception as exc:  # noqa: BLE001
                log.warning("keycloak logout failed: %s", exc)
            await sessions.delete(current_sid)
    response = JSONResponse({"status": "logged_out"})
    _clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Прокси к защищенным API: фронтенд -> (cookie) -> bionicpro-auth -> (Bearer) -> reports-api
# ---------------------------------------------------------------------------
@app.api_route("/api/{path:path}", methods=["GET"])
async def api_proxy(path: str, request: Request) -> Response:
    ctx = await _require_session(request)
    async with httpx.AsyncClient(timeout=60) as client:
        upstream = await client.get(
            f"{settings.reports_api_url}/{path}",
            params=dict(request.query_params),
            headers={"Authorization": f"Bearer {ctx.session.access_token}"},
        )
    response = Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )
    return _with_rotated_cookie(response, ctx)


@app.get("/healthz")
async def healthz() -> dict:
    await sessions.ping()
    return {"status": "ok"}
