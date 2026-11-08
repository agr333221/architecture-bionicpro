"""Back-channel взаимодействие с Keycloak: обмен кода, refresh, logout, проверка id_token."""
import time

import httpx
import jwt

from .config import settings
from .sessions import Session

_jwks = jwt.PyJWKClient(settings.jwks_uri, cache_keys=True, lifespan=300)


class KeycloakError(Exception):
    pass


def _client_auth() -> httpx.BasicAuth:
    return httpx.BasicAuth(settings.client_id, settings.client_secret)


async def exchange_code(code: str, code_verifier: str) -> dict:
    """Authorization Code + PKCE: обмен кода на токены с передачей code_verifier."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            settings.token_endpoint,
            auth=_client_auth(),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.redirect_uri,
                "code_verifier": code_verifier,
            },
        )
    if resp.status_code != 200:
        raise KeycloakError(f"code exchange failed: {resp.status_code} {resp.text}")
    return resp.json()


async def refresh(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            settings.token_endpoint,
            auth=_client_auth(),
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
    if resp.status_code != 200:
        raise KeycloakError(f"refresh failed: {resp.status_code} {resp.text}")
    return resp.json()


async def logout(refresh_token: str) -> None:
    """Завершает SSO-сессию в Keycloak и отзывает refresh_token."""
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            settings.logout_endpoint,
            auth=_client_auth(),
            data={"refresh_token": refresh_token},
        )


def verify_jwt(token: str, audience: str | None = None, nonce: str | None = None) -> dict:
    key = _jwks.get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        key.key,
        algorithms=["RS256"],
        issuer=settings.issuer,
        audience=audience,
        options={"verify_aud": audience is not None},
        leeway=10,
    )
    if nonce is not None and claims.get("nonce") != nonce:
        raise KeycloakError("nonce mismatch")
    return claims


def session_from_tokens(tokens: dict, nonce: str | None = None) -> Session:
    id_token = tokens.get("id_token")
    id_claims = verify_jwt(id_token, audience=settings.client_id, nonce=nonce) if id_token else {}
    # access_token тоже проверяем подписью: из него берем роли
    access_claims = verify_jwt(tokens["access_token"])
    now = time.time()
    session = Session(
        sub=access_claims["sub"],
        username=access_claims.get("preferred_username", id_claims.get("preferred_username", "")),
        email=access_claims.get("email"),
        name=access_claims.get("name"),
        roles=sorted(access_claims.get("realm_access", {}).get("roles", [])),
        access_token=tokens["access_token"],
        access_expires_at=now + int(tokens.get("expires_in", 60)),
        refresh_token_enc="",
        refresh_expires_at=now + int(tokens.get("refresh_expires_in", settings.session_idle_ttl)),
        id_token=id_token,
    )
    session.set_refresh_token(tokens["refresh_token"])
    return session


def apply_refreshed_tokens(session: Session, tokens: dict) -> None:
    access_claims = verify_jwt(tokens["access_token"])
    if access_claims["sub"] != session.sub:
        raise KeycloakError("subject changed on refresh")
    now = time.time()
    session.access_token = tokens["access_token"]
    session.access_expires_at = now + int(tokens.get("expires_in", 60))
    session.roles = sorted(access_claims.get("realm_access", {}).get("roles", []))
    if tokens.get("refresh_token"):
        session.set_refresh_token(tokens["refresh_token"])
        session.refresh_expires_at = now + int(tokens.get("refresh_expires_in", settings.session_idle_ttl))
    if tokens.get("id_token"):
        session.id_token = tokens["id_token"]
