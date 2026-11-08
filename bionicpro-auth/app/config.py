"""Настройки сервиса bionicpro-auth (читаются из переменных окружения)."""
import base64
import os


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


class Settings:
    # Keycloak: публичный адрес (для браузера) и внутренний адрес (для back-channel запросов)
    keycloak_public_url = _env("KEYCLOAK_PUBLIC_URL", "http://localhost:8080").rstrip("/")
    keycloak_internal_url = _env("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080").rstrip("/")
    realm = _env("KEYCLOAK_REALM", "reports-realm")
    client_id = _env("KEYCLOAK_CLIENT_ID", "bionicpro-auth")
    client_secret = _env("KEYCLOAK_CLIENT_SECRET")

    # Куда Keycloak возвращает пользователя после логина (через nginx фронтенда)
    redirect_uri = _env("AUTH_REDIRECT_URI", "http://localhost:3000/auth/callback")
    frontend_url = _env("FRONTEND_URL", "http://localhost:3000").rstrip("/")

    redis_url = _env("REDIS_URL", "redis://redis:6379/0")

    # 32 байта в base64: ключ AES-256-GCM для шифрования refresh_token
    token_enc_key = base64.b64decode(_env("TOKEN_ENC_KEY"))

    cookie_name = _env("SESSION_COOKIE_NAME", "bionicpro_session")
    cookie_secure = _env("SESSION_COOKIE_SECURE", "true").lower() == "true"
    # Время жизни сессии без активности. Должно быть больше времени жизни access_token (120 c)
    session_idle_ttl = int(_env("SESSION_IDLE_TTL", "1800"))
    # Сколько секунд старый session id после ротации еще ведет на новую сессию
    # (защита от гонки параллельных запросов фронтенда)
    rotation_grace_ttl = int(_env("SESSION_ROTATION_GRACE", "10"))
    # Обновлять access_token заранее, если до истечения осталось меньше N секунд
    access_token_skew = int(_env("ACCESS_TOKEN_REFRESH_SKEW", "15"))

    reports_api_url = _env("REPORTS_API_URL", "http://reports-api:8001").rstrip("/")

    @property
    def issuer(self) -> str:
        return f"{self.keycloak_public_url}/realms/{self.realm}"

    @property
    def authorize_endpoint(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/auth"

    @property
    def _internal_oidc(self) -> str:
        return f"{self.keycloak_internal_url}/realms/{self.realm}/protocol/openid-connect"

    @property
    def token_endpoint(self) -> str:
        return f"{self._internal_oidc}/token"

    @property
    def logout_endpoint(self) -> str:
        return f"{self._internal_oidc}/logout"

    @property
    def revoke_endpoint(self) -> str:
        return f"{self._internal_oidc}/revoke"

    @property
    def jwks_uri(self) -> str:
        return f"{self._internal_oidc}/certs"


settings = Settings()

if len(settings.token_enc_key) != 32:
    raise RuntimeError("TOKEN_ENC_KEY must be 32 bytes encoded in base64")
