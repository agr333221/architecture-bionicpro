"""End-to-end проверка стенда BionicPRO без браузера.

Эмулирует браузер: вход через bionicpro-auth -> Keycloak (логин, настройка/ввод TOTP,
consent) -> callback, затем проверяет сессии, ротацию session id, refresh access_token,
доступ к отчетам и CDN.

Запуск:
    pip install requests pyotp
    python tests/e2e_check.py                    # все проверки
    python tests/e2e_check.py --with-refresh     # + ожидание истечения access_token (~2.5 мин)

OTP-секреты, созданные при первой настройке TOTP, сохраняются в tests/.otp_secrets.json
(файл в .gitignore), чтобы повторные входы проходили с вводом одноразового пароля.
"""
import argparse
import base64
import html
import http.cookiejar
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import pyotp
import requests

FRONTEND = "http://localhost:3000"
COOKIE = "bionicpro_session"
SECRETS_FILE = Path(__file__).with_name(".otp_secrets.json")

RESULTS: list[tuple[str, bool, str]] = []
USED_OTP: dict[str, str] = {}


def fresh_otp(username: str, secret: str) -> str:
    """Keycloak не принимает повторно уже использованный код: ждем следующее окно TOTP."""
    totp = pyotp.TOTP(secret)
    while totp.now() == USED_OTP.get(username):
        time.sleep(1)
    USED_OTP[username] = totp.now()
    return USED_OTP[username]


def check(name: str, ok: bool, details: str = "") -> bool:
    RESULTS.append((name, ok, details))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {details}" if details else ""))
    return ok


# Браузеры считают http://localhost безопасным контекстом и отправляют Secure-cookie,
# а атрибут Version=1 (его ставит Keycloak) игнорируют. Стандартный cookiejar Python
# так не делает. requests создает собственные jar-ы с политикой по умолчанию,
# поэтому поведение браузера эмулируем на уровне класса политики.
http.cookiejar.DefaultCookiePolicy.return_ok_secure = lambda self, cookie, request: True
http.cookiejar.DefaultCookiePolicy.return_ok_version = lambda self, cookie, request: True


def new_browser() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "bionicpro-e2e-check"
    return s


def load_secrets() -> dict:
    return json.loads(SECRETS_FILE.read_text()) if SECRETS_FILE.exists() else {}


def save_secret(username: str, secret: str) -> None:
    data = load_secrets()
    data[username] = secret
    SECRETS_FILE.write_text(json.dumps(data, indent=2))


KEYCLOAK = "http://localhost:8080"
REALM = "reports-realm"


def reset_otp(username: str) -> None:
    """Если OTP-секрет пользователя неизвестен тесту, сбрасывает его OTP через Admin API
    (пользователь снова пройдет настройку TOTP). Учетка администратора - из окружения."""
    if username in load_secrets():
        return
    import os
    tok = requests.post(f"{KEYCLOAK}/realms/master/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": "admin-cli",
        "username": os.getenv("KEYCLOAK_ADMIN", "admin"),
        "password": os.getenv("KEYCLOAK_ADMIN_PASSWORD", "admin")}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    base = f"{KEYCLOAK}/admin/realms/{REALM}"
    user = requests.get(f"{base}/users", params={"username": username, "exact": "true"}, headers=h).json()
    if not user:
        return
    uid = user[0]["id"]
    for c in requests.get(f"{base}/users/{uid}/credentials", headers=h).json():
        if c["type"] == "otp":
            requests.delete(f"{base}/users/{uid}/credentials/{c['id']}", headers=h)


def form_of(page: str, form_id: str | None = None) -> tuple[str, dict]:
    pattern = r'<form[^>]*id="%s"[^>]*>(.*?)</form>' % form_id if form_id else r"<form[^>]*>(.*?)</form>"
    m = re.search(pattern, page, re.S)
    if not m:
        raise RuntimeError(f"form {form_id} not found")
    tag = re.search(r"<form[^>]*>", m.group(0)).group(0)
    action = html.unescape(re.search(r'action="([^"]+)"', tag).group(1))
    fields = {}
    for inp in re.findall(r"<input[^>]*>", m.group(1)):
        name = re.search(r'name="([^"]+)"', inp)
        value = re.search(r'value="([^"]*)"', inp)
        if name and 'type="submit"' not in inp:
            fields[name.group(1)] = html.unescape(value.group(1)) if value else ""
    return action, fields


def follow(s: requests.Session, resp: requests.Response) -> requests.Response:
    """Идет по редиректам вручную, пока не дойдет до страницы или до фронтенда."""
    for _ in range(15):
        if resp.status_code not in (301, 302, 303):
            return resp
        location = urljoin(resp.url, resp.headers["Location"])
        resp = s.get(location, allow_redirects=False)
    raise RuntimeError("too many redirects")


def login(username: str, password: str) -> requests.Session:
    """Полный PKCE-логин через bionicpro-auth. Возвращает 'браузер' с сессионной cookie."""
    reset_otp(username)
    s = new_browser()
    r = s.get(f"{FRONTEND}/auth/login", allow_redirects=False)
    auth_url = r.headers["Location"]
    q = parse_qs(urlparse(auth_url).query)
    check(f"{username}: authorize request uses PKCE S256",
          q.get("code_challenge_method") == ["S256"] and len(q.get("code_challenge", [""])[0]) == 43,
          f"client_id={q.get('client_id')}")
    login_cookie = r.cookies.get("bionicpro_login_state")
    check(f"{username}: login state cookie is HttpOnly",
          "httponly" in r.headers.get("set-cookie", "").lower() and login_cookie is not None)

    page = follow(s, r)
    action, fields = form_of(page.text, "kc-form-login")
    fields.update({"username": username, "password": password})
    page = follow(s, s.post(urljoin(page.url, action), data=fields, allow_redirects=False))

    for _ in range(6):
        if page.url.startswith(FRONTEND):
            break
        text = page.text
        if 'id="kc-totp-settings-form"' in text:
            action, fields = form_of(text, "kc-totp-settings-form")
            # Тот же ключ, что пользователь сканирует QR-кодом в Google Authenticator / FreeOTP
            secret = base64.b32encode(fields["totpSecret"].encode()).decode().rstrip("=")
            save_secret(username, secret)
            fields["totp"] = fresh_otp(username, secret)
            fields["userLabel"] = "e2e"
            check(f"{username}: Keycloak requires OTP setup on first login", True)
            page = follow(s, s.post(urljoin(page.url, action), data=fields, allow_redirects=False))
        elif 'id="kc-otp-login-form"' in text:
            secret = load_secrets().get(username)
            if not secret:
                raise RuntimeError(f"no stored OTP secret for {username}")
            action, fields = form_of(text, "kc-otp-login-form")
            fields["otp"] = fresh_otp(username, secret)
            check(f"{username}: Keycloak asks one-time password", True)
            page = follow(s, s.post(urljoin(page.url, action), data=fields, allow_redirects=False))
        elif "kc-oauth" in text or 'name="accept"' in text:
            action, fields = form_of(text)
            fields["accept"] = "Yes"
            check(f"{username}: consent screen shown", True)
            page = follow(s, s.post(urljoin(page.url, action), data=fields, allow_redirects=False))
        else:
            err = re.search(r'kc-feedback-text">([^<]+)<', text) or re.search(r"<title>([^<]+)</title>", text)
            raise RuntimeError(f"unexpected Keycloak page: {page.url} {err.group(1) if err else ''}")

    sid = s.cookies.get(COOKIE)
    check(f"{username}: session cookie issued after login", page.url.startswith(FRONTEND) and bool(sid))
    return s


def set_cookie_header(resp: requests.Response) -> str:
    return resp.headers.get("set-cookie", "")


def run_auth_checks(args) -> None:
    # --- без сессии ---
    r = requests.get(f"{FRONTEND}/auth/me")
    check("no cookie -> /auth/me 401", r.status_code == 401)
    r = requests.get(f"{FRONTEND}/api/reports")
    check("no cookie -> /api/reports 401", r.status_code == 401)

    s = login("prothetic1", "prothetic123")

    # --- cookie flags и отсутствие токенов во фронтенде ---
    r = s.get(f"{FRONTEND}/auth/me")
    sc = set_cookie_header(r).lower()
    check("session cookie has HttpOnly, Secure, SameSite", all(x in sc for x in ("httponly", "secure", "samesite=lax")), sc)
    body = r.json()
    check("/auth/me returns profile without tokens",
          r.status_code == 200 and "access_token" not in body and "refresh_token" not in body,
          f"user={body.get('username')} roles={body.get('roles')}")
    check("access_token lifetime =< 120 s", body.get("access_token_expires_in", 999) <= 120,
          f"expires_in={body.get('access_token_expires_in')}")

    # --- ротация session id ---
    old_sid = s.cookies.get(COOKIE)
    s.get(f"{FRONTEND}/auth/me")
    new_sid = s.cookies.get(COOKIE)
    check("session id rotated on each protected request", old_sid != new_sid)
    time.sleep(11)
    r = requests.get(f"{FRONTEND}/auth/me", cookies={COOKIE: old_sid})
    check("old session id is rejected after grace period (session fixation)", r.status_code == 401)
    r = s.get(f"{FRONTEND}/auth/me")
    check("current session still valid", r.status_code == 200)

    if args.with_refresh:
        print("waiting 130 s for access_token to expire...")
        time.sleep(130)
        r = s.get(f"{FRONTEND}/auth/me")
        check("expired access_token transparently refreshed by bionicpro-auth",
              r.status_code == 200 and r.json()["access_token_expires_in"] > 100,
              f"expires_in={r.json().get('access_token_expires_in') if r.ok else r.status_code}")

    # --- logout ---
    r = s.post(f"{FRONTEND}/auth/logout")
    check("logout clears session", r.status_code == 200 and s.get(f"{FRONTEND}/auth/me").status_code == 401)

    # --- повторный вход: OTP обязателен ---
    login("prothetic1", "prothetic123")

    # --- LDAP-пользователь представительства ---
    s_ldap = login("john.doe", "password")
    me = s_ldap.get(f"{FRONTEND}/auth/me").json()
    check("LDAP user john.doe has role prothetic_user from LDAP group", "prothetic_user" in me["roles"], str(me["roles"]))


def run_report_checks(args) -> None:
    s = login("prothetic1", "prothetic123")

    r = s.get(f"{FRONTEND}/api/reports")
    if not check("report request ok", r.status_code == 200, f"{r.status_code} {r.text[:200]}"):
        return
    link = r.json()
    check("report period limited by ETL watermark", link["period"]["to"] <= link["available_until"],
          f"period={link['period']} available_until={link['available_until']}")

    cdn = requests.get(link["url"])
    check("report downloaded from CDN", cdn.status_code == 200, f"X-Cache-Status={cdn.headers.get('X-Cache-Status')}")
    report = cdn.json()
    check("report belongs to the user", report["customer"]["email"] == "prothetic1@example.com",
          f"rows={len(report['daily'])} summary={len(report['summary'])}")
    cdn2 = requests.get(link["url"])
    check("second CDN request served from nginx cache", cdn2.headers.get("X-Cache-Status") == "HIT",
          cdn2.headers.get("X-Cache-Status", ""))

    r2 = s.get(f"{FRONTEND}/api/reports").json()
    check("repeated report request served from S3 without OLAP query", r2["cache"] == "hit", f"cache={r2['cache']}")

    r = s.get(f"{FRONTEND}/api/reports", params={"user": "prothetic2@example.com"})
    check("request for another user's report -> 403", r.status_code == 403, r.text[:120])

    r = s.get(f"{FRONTEND}/api/reports", params={"date_from": "2099-01-01", "date_to": "2099-01-31"})
    check("period not processed by ETL -> 409", r.status_code == 409, r.text[:160])

    tampered = link["url"].replace("X-Amz-Signature=", "X-Amz-Signature=00")
    bad = requests.get(tampered.replace(link["url"].split("/reports/")[1].split("/")[1], "deadbeef" * 4))
    check("unknown/forged CDN link -> denied", bad.status_code in (403, 404), str(bad.status_code))

    s_user = login("user1", "password123")
    r = s_user.get(f"{FRONTEND}/api/reports")
    check("user without prothetic_user role -> 403", r.status_code == 403, r.text[:120])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-refresh", action="store_true")
    parser.add_argument("--only", choices=["auth", "reports"])
    args = parser.parse_args()
    if args.only in (None, "auth"):
        run_auth_checks(args)
    if args.only in (None, "reports"):
        run_report_checks(args)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
