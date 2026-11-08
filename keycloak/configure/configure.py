"""Идемпотентная настройка realm reports-realm через Keycloak Admin REST API.

Что настраивается (задание 1):
  - время жизни access_token = 120 c, refresh_token / SSO-сессии;
  - PKCE (S256) для клиентов bionicpro-auth и reports-frontend;
  - конфиденциальный клиент bionicpro-auth (BFF) с consent и audience reports-api;
  - LDAP User Federation (OpenLDAP представительства) + маппинг ролей из LDAP-групп;
  - MFA: обязательный OTP (TOTP) для всех пользователей;
  - Identity Brokering: Яндекс ID (generic OAuth 2.0 provider) + мапперы профиля;
  - экспорт realm после всех изменений в keycloak/keycloak-results-export.json.

Скрипт можно запускать повторно: существующие объекты обновляются, а не дублируются.
"""
import json
import os
import sys
import time

import requests

KC = os.getenv("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/")
ADMIN_USER = os.getenv("KEYCLOAK_ADMIN", "admin")
ADMIN_PASSWORD = os.getenv("KEYCLOAK_ADMIN_PASSWORD", "admin")
REALM = os.getenv("KEYCLOAK_REALM", "reports-realm")

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
AUTH_CLIENT_ID = os.getenv("BIONICPRO_AUTH_CLIENT_ID", "bionicpro-auth")
AUTH_CLIENT_SECRET = os.environ["BIONICPRO_AUTH_CLIENT_SECRET"]

LDAP_URL = os.getenv("LDAP_URL", "ldap://openldap:389")
LDAP_BIND_DN = os.getenv("LDAP_BIND_DN", "cn=admin,dc=example,dc=com")
LDAP_BIND_PASSWORD = os.getenv("LDAP_BIND_PASSWORD", "admin")
LDAP_BASE_DN = os.getenv("LDAP_BASE_DN", "dc=example,dc=com")
LDAP_PROVIDER_NAME = os.getenv("LDAP_PROVIDER_NAME", "ldap-bionicpro-eu")
LDAP_REPRESENTATION = os.getenv("LDAP_REPRESENTATION", "EU")

YANDEX_CLIENT_ID = os.getenv("YANDEX_CLIENT_ID", "")
YANDEX_CLIENT_SECRET = os.getenv("YANDEX_CLIENT_SECRET", "")

EXPORT_FILE = os.getenv("EXPORT_FILE", "/export/keycloak-results-export.json")

S = requests.Session()


def log(msg: str) -> None:
    print(f"[configure] {msg}", flush=True)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def wait_for_keycloak(timeout: int = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if S.get(f"{KC}/realms/{REALM}/.well-known/openid-configuration", timeout=5).status_code == 200:
                return
        except requests.RequestException:
            pass
        log("waiting for keycloak...")
        time.sleep(5)
    sys.exit("keycloak is not ready")


def login() -> None:
    r = S.post(
        f"{KC}/realms/master/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": "admin-cli",
              "username": ADMIN_USER, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    r.raise_for_status()
    S.headers["Authorization"] = f"Bearer {r.json()['access_token']}"


def api(method: str, path: str, **kwargs):
    url = f"{KC}/admin/realms/{REALM}{path}" if not path.startswith("http") else path
    r = S.request(method, url, timeout=30, **kwargs)
    if r.status_code == 401:
        login()
        r = S.request(method, url, timeout=30, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text}")
    if r.content and "json" in r.headers.get("content-type", ""):
        return r.json()
    return r


def created_id(resp) -> str:
    return resp.headers["Location"].rstrip("/").split("/")[-1]


# ---------------------------------------------------------------------------
# 1. Realm: время жизни токенов и сессий
# ---------------------------------------------------------------------------
def configure_realm() -> None:
    realm = api("GET", "")
    realm.update({
        "accessTokenLifespan": 120,            # access_token не более 2 минут
        "accessTokenLifespanForImplicitFlow": 120,
        "ssoSessionIdleTimeout": 1800,          # refresh_token живет 30 минут без активности
        "ssoSessionMaxLifespan": 36000,
        "revokeRefreshToken": False,
        "bruteForceProtected": True,
        "failureFactor": 10,
        "internationalizationEnabled": True,
        "supportedLocales": ["ru", "en"],
        "defaultLocale": "ru",
        # Политика OTP, совместимая с Google Authenticator и FreeOTP
        "otpPolicyType": "totp",
        "otpPolicyAlgorithm": "HmacSHA1",
        "otpPolicyDigits": 6,
        "otpPolicyPeriod": 30,
        "otpPolicyLookAheadWindow": 1,
    })
    api("PUT", "", json=realm)
    log("realm token lifespans: access=120s, sso idle=1800s")


# ---------------------------------------------------------------------------
# 2. Клиенты: PKCE, BFF, audience
# ---------------------------------------------------------------------------
def find_client(client_id: str) -> dict | None:
    found = api("GET", "/clients", params={"clientId": client_id})
    return found[0] if found else None


def upsert_client(rep: dict) -> str:
    existing = find_client(rep["clientId"])
    if existing:
        existing.update(rep)
        api("PUT", f"/clients/{existing['id']}", json=existing)
        return existing["id"]
    return created_id(api("POST", "/clients", json=rep))


def ensure_protocol_mapper(client_uuid: str, mapper: dict) -> None:
    mappers = api("GET", f"/clients/{client_uuid}/protocol-mappers/models")
    for m in mappers:
        if m["name"] == mapper["name"]:
            mapper["id"] = m["id"]
            api("PUT", f"/clients/{client_uuid}/protocol-mappers/models/{m['id']}", json=mapper)
            return
    api("POST", f"/clients/{client_uuid}/protocol-mappers/models", json=mapper)


def configure_clients() -> None:
    # BFF bionicpro-auth: конфиденциальный клиент, Authorization Code + PKCE (S256 обязателен)
    auth_uuid = upsert_client({
        "clientId": AUTH_CLIENT_ID,
        "name": "BionicPRO auth (BFF)",
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "clientAuthenticatorType": "client-secret",
        "secret": AUTH_CLIENT_SECRET,
        "standardFlowEnabled": True,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        # Сервис спрашивает у пользователя разрешение на использование данных профиля
        "consentRequired": True,
        "redirectUris": [f"{FRONTEND_URL}/auth/callback"],
        "webOrigins": [],
        "frontchannelLogout": True,
        "attributes": {
            "pkce.code.challenge.method": "S256",
            "post.logout.redirect.uris": f"{FRONTEND_URL}/*",
            "use.refresh.tokens": "true",
            "client_credentials.use_refresh_token": "false",
            "display.on.consent.screen": "true",
            "consent.screen.text": "BionicPRO: доступ к профилю и отчетам о работе протеза",
        },
    })
    # access_token должен содержать aud=reports-api, его проверяет сервис отчетов
    ensure_protocol_mapper(auth_uuid, {
        "name": "audience-reports-api",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.client.audience": "reports-api",
            "access.token.claim": "true",
            "id.token.claim": "false",
            "introspection.token.claim": "true",
        },
    })
    # Full scope выключен: в токен попадают только явно разрешенные клиенту роли realm
    client = api("GET", f"/clients/{auth_uuid}")
    if client.get("fullScopeAllowed"):
        client["fullScopeAllowed"] = False
        api("PUT", f"/clients/{auth_uuid}", json=client)
    allowed = [api("GET", f"/roles/{name}") for name in ("user", "prothetic_user", "administrator")]
    mapped = {r["name"] for r in api("GET", f"/clients/{auth_uuid}/scope-mappings/realm")}
    missing = [r for r in allowed if r["name"] not in mapped]
    if missing:
        api("POST", f"/clients/{auth_uuid}/scope-mappings/realm", json=missing)
    log(f"client {AUTH_CLIENT_ID}: confidential + PKCE S256 + consent + audience reports-api, "
        f"role scope: {[r['name'] for r in allowed]}")

    # Исходный публичный клиент фронтенда: тоже только PKCE, без password grant
    front = find_client("reports-frontend")
    if front:
        front["directAccessGrantsEnabled"] = False
        front["implicitFlowEnabled"] = False
        front.setdefault("attributes", {})["pkce.code.challenge.method"] = "S256"
        api("PUT", f"/clients/{front['id']}", json=front)
        log("client reports-frontend: PKCE S256 required, direct access grants disabled")


# ---------------------------------------------------------------------------
# 3. LDAP User Federation + маппинг ролей
# ---------------------------------------------------------------------------
def realm_id() -> str:
    return api("GET", "")["id"]


def configure_ldap() -> None:
    parent = realm_id()
    comps = api("GET", "/components", params={"type": "org.keycloak.storage.UserStorageProvider",
                                              "name": LDAP_PROVIDER_NAME})
    config = {
        "enabled": ["true"],
        "priority": ["0"],
        "vendor": ["other"],
        "connectionUrl": [LDAP_URL],
        "bindDn": [LDAP_BIND_DN],
        "bindCredential": [LDAP_BIND_PASSWORD],
        "authType": ["simple"],
        "usersDn": [f"ou=People,{LDAP_BASE_DN}"],
        "userObjectClasses": ["inetOrgPerson"],
        "usernameLDAPAttribute": ["uid"],
        "rdnLDAPAttribute": ["uid"],
        "uuidLDAPAttribute": ["entryUUID"],
        "searchScope": ["1"],
        "editMode": ["READ_ONLY"],
        "importEnabled": ["true"],
        "syncRegistrations": ["false"],
        "trustEmail": ["true"],
        "pagination": ["false"],
        "useTruststoreSpi": ["always"],
        "connectionPooling": ["true"],
        "fullSyncPeriod": ["3600"],
        "changedSyncPeriod": ["300"],
        "batchSizeForSync": ["1000"],
        "cachePolicy": ["DEFAULT"],
    }
    rep = {
        "name": LDAP_PROVIDER_NAME,
        "providerId": "ldap",
        "providerType": "org.keycloak.storage.UserStorageProvider",
        "parentId": parent,
        "config": config,
    }
    if comps:
        ldap_id = comps[0]["id"]
        rep["id"] = ldap_id
        api("PUT", f"/components/{ldap_id}", json=rep)
    else:
        ldap_id = created_id(api("POST", "/components", json=rep))
    log(f"LDAP federation {LDAP_PROVIDER_NAME} -> {LDAP_URL}")

    def upsert_mapper(name: str, provider_id: str, cfg: dict) -> str:
        existing = api("GET", "/components", params={"parent": ldap_id, "name": name})
        mrep = {
            "name": name,
            "providerId": provider_id,
            "providerType": "org.keycloak.storage.ldap.mappers.LDAPStorageMapper",
            "parentId": ldap_id,
            "config": cfg,
        }
        if existing:
            mrep["id"] = existing[0]["id"]
            api("PUT", f"/components/{mrep['id']}", json=mrep)
            return mrep["id"]
        return created_id(api("POST", "/components", json=mrep))

    # Группы LDAP (ou=Groups) -> роли realm с теми же именами: user, prothetic_user.
    # Так роли представительства синхронизируются с ролевой моделью BionicPRO.
    roles_mapper = upsert_mapper("ldap-groups-to-realm-roles", "role-ldap-mapper", {
        "roles.dn": [f"ou=Groups,{LDAP_BASE_DN}"],
        "role.name.ldap.attribute": ["cn"],
        "role.object.classes": ["groupOfNames"],
        "membership.ldap.attribute": ["member"],
        "membership.attribute.type": ["DN"],
        "membership.user.ldap.attribute": ["uid"],
        "roles.ldap.filter": [""],
        "mode": ["READ_ONLY"],
        "user.roles.retrieve.strategy": ["LOAD_ROLES_BY_MEMBER_ATTRIBUTE"],
        "memberof.ldap.attribute": ["memberOf"],
        "use.realm.roles.mapping": ["true"],
        "client.id": [""],
    })
    # Признак представительства (страны), из которой пришел пользователь
    upsert_mapper("representation", "hardcoded-attribute-mapper", {
        "user.model.attribute": ["representation"],
        "attribute.value": [LDAP_REPRESENTATION],
    })

    # Синхронизация пользователей и ролей из LDAP
    r = api("POST", f"/user-storage/{ldap_id}/sync", params={"action": "triggerFullSync"})
    log(f"LDAP users sync: {r if isinstance(r, dict) else r.status_code}")
    r = api("POST", f"/user-storage/{ldap_id}/mappers/{roles_mapper}/sync", params={"direction": "fedToKeycloak"})
    log(f"LDAP roles sync: {r if isinstance(r, dict) else r.status_code}")


# ---------------------------------------------------------------------------
# 4. MFA: обязательный OTP для всех пользователей
# ---------------------------------------------------------------------------
def ensure_flow_copy(source: str, new_alias: str) -> None:
    flows = api("GET", "/authentication/flows")
    if not any(f["alias"] == new_alias for f in flows):
        api("POST", f"/authentication/flows/{requests.utils.quote(source)}/copy", json={"newName": new_alias})


def set_requirement(flow_alias: str, execution: dict, requirement: str) -> None:
    if execution["requirement"] != requirement:
        execution["requirement"] = requirement
        api("PUT", f"/authentication/flows/{requests.utils.quote(flow_alias)}/executions", json=execution)


def configure_mfa() -> None:
    # 4.1 Required action CONFIGURE_TOTP включена и назначается всем новым пользователям
    ra = api("GET", "/authentication/required-actions/CONFIGURE_TOTP")
    ra.update({"enabled": True, "defaultAction": True})
    api("PUT", "/authentication/required-actions/CONFIGURE_TOTP", json=ra)

    # 4.2 Браузерный flow, в котором OTP обязателен (а не условный)
    ensure_flow_copy("browser", "browser-mfa")
    executions = api("GET", "/authentication/flows/browser-mfa/executions")
    otp = next(e for e in executions if e.get("providerId") == "auth-otp-form")
    # Подпоток, в котором находится OTP Form (по умолчанию он CONDITIONAL)
    idx = executions.index(otp)
    parent = next(e for e in reversed(executions[:idx])
                  if e["level"] == otp["level"] - 1 and e.get("authenticationFlow"))
    for e in executions[executions.index(parent) + 1:]:
        if e["level"] <= parent["level"]:
            break
        if e["level"] != otp["level"]:
            continue
        if e.get("providerId", "").startswith("conditional-"):
            set_requirement("browser-mfa", e, "DISABLED")   # условие больше не нужно
        elif e is otp:
            set_requirement("browser-mfa", e, "REQUIRED")
        else:
            set_requirement("browser-mfa", e, "DISABLED")   # альтернативы (WebAuthn и т.п.)
    set_requirement("browser-mfa", parent, "REQUIRED")

    realm = api("GET", "")
    realm["browserFlow"] = "browser-mfa"
    api("PUT", "", json=realm)

    # 4.3 Flow после входа через внешний IdP (Яндекс): тоже обязателен OTP
    flows = api("GET", "/authentication/flows")
    if not any(f["alias"] == "post-broker-otp" for f in flows):
        api("POST", "/authentication/flows", json={
            "alias": "post-broker-otp",
            "description": "OTP after login via external identity provider",
            "providerId": "basic-flow",
            "topLevel": True,
            "builtIn": False,
        })
        api("POST", "/authentication/flows/post-broker-otp/executions/execution",
            json={"provider": "auth-otp-form"})
    for e in api("GET", "/authentication/flows/post-broker-otp/executions"):
        set_requirement("post-broker-otp", e, "REQUIRED")

    # 4.4 Уже существующим пользователям тоже назначаем настройку OTP
    for user in api("GET", "/users", params={"max": 1000, "briefRepresentation": "false"}):
        creds = api("GET", f"/users/{user['id']}/credentials")
        if any(c["type"] == "otp" for c in creds):
            continue
        actions = set(user.get("requiredActions") or [])
        if "CONFIGURE_TOTP" not in actions:
            actions.add("CONFIGURE_TOTP")
            api("PUT", f"/users/{user['id']}", json={"requiredActions": sorted(actions)})
    log("MFA: OTP required in browser-mfa and post-broker-otp flows, CONFIGURE_TOTP is default action")


# ---------------------------------------------------------------------------
# 5. Identity Brokering: Яндекс ID
# ---------------------------------------------------------------------------
YANDEX_ALIAS = "yandex"


def configure_yandex() -> None:
    if not YANDEX_CLIENT_ID or not YANDEX_CLIENT_SECRET:
        log("YANDEX_CLIENT_ID/SECRET are not set: Yandex ID provider skipped")
        return
    rep = {
        "alias": YANDEX_ALIAS,
        "displayName": "Яндекс ID",
        "providerId": "oauth2",           # generic OAuth 2.0 provider (Keycloak 26.3+)
        "enabled": True,
        "trustEmail": True,               # email подтвержден Яндексом
        "storeToken": False,
        "addReadTokenRoleOnCreate": False,
        "linkOnly": False,
        "hideOnLogin": False,
        "firstBrokerLoginFlowAlias": "first broker login",
        "postBrokerLoginFlowAlias": "post-broker-otp",
        "config": {
            "clientId": YANDEX_CLIENT_ID,
            "clientSecret": YANDEX_CLIENT_SECRET,
            "clientAuthMethod": "client_secret_post",
            "authorizationUrl": "https://oauth.yandex.ru/authorize",
            "tokenUrl": "https://oauth.yandex.ru/token",
            "userInfoUrl": "https://login.yandex.ru/info?format=json",
            "defaultScope": "login:info login:email login:avatar",
            "pkceEnabled": "true",
            "pkceMethod": "S256",
            # Поля ответа https://login.yandex.ru/info
            "userIDClaim": "id",
            "userNameClaim": "login",
            "emailClaim": "default_email",
            "givenNameClaim": "first_name",
            "familyNameClaim": "last_name",
            "fullNameClaim": "real_name",
            # FORCE: профиль из Яндекса обновляется в БД Keycloak при каждом входе
            "syncMode": "FORCE",
            "prompt": "consent",
            "guiOrder": "1",
        },
    }
    existing = [p for p in api("GET", "/identity-provider/instances") if p["alias"] == YANDEX_ALIAS]
    if existing:
        api("PUT", f"/identity-provider/instances/{YANDEX_ALIAS}", json=rep)
    else:
        api("POST", "/identity-provider/instances", json=rep)

    def upsert_idp_mapper(name: str, mapper_type: str, cfg: dict) -> None:
        mappers = api("GET", f"/identity-provider/instances/{YANDEX_ALIAS}/mappers")
        mrep = {"name": name, "identityProviderAlias": YANDEX_ALIAS,
                "identityProviderMapper": mapper_type, "config": {"syncMode": "FORCE", **cfg}}
        for m in mappers:
            if m["name"] == name:
                mrep["id"] = m["id"]
                api("PUT", f"/identity-provider/instances/{YANDEX_ALIAS}/mappers/{m['id']}", json=mrep)
                return
        api("POST", f"/identity-provider/instances/{YANDEX_ALIAS}/mappers", json=mrep)

    # Данные профиля Яндекса сохраняются как атрибуты пользователя в БД Keycloak
    for claim, attribute in [("login", "yandex_login"), ("id", "yandex_id"),
                             ("display_name", "yandex_display_name"),
                             ("default_avatar_id", "yandex_avatar_id"), ("sex", "yandex_sex"),
                             ("birthday", "yandex_birthday")]:
        upsert_idp_mapper(f"yandex-{attribute}", "oidc-user-attribute-idp-mapper",
                          {"claim": claim, "user.attribute": attribute})
    upsert_idp_mapper("yandex-source", "hardcoded-attribute-idp-mapper",
                      {"attribute": "identity_source", "attribute.value": "yandex"})
    # Пользователь Яндекс ID получает базовую роль user (отчеты требуют prothetic_user)
    upsert_idp_mapper("yandex-default-role", "oidc-hardcoded-role-idp-mapper", {"role": "user"})
    log("Yandex ID identity provider configured (oauth2, PKCE, consent)")


# ---------------------------------------------------------------------------
# 6. Экспорт realm
# ---------------------------------------------------------------------------
def export_realm() -> None:
    data = api("POST", "/partial-export", params={"exportClients": "true", "exportGroupsAndRoles": "true"})
    with open(EXPORT_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    log(f"realm exported to {EXPORT_FILE} (secrets are masked by Keycloak)")


def main() -> None:
    wait_for_keycloak()
    login()
    configure_realm()
    configure_clients()
    configure_ldap()
    configure_mfa()
    configure_yandex()
    export_realm()
    log("done")


if __name__ == "__main__":
    main()
