"""Настройки reports-api."""
import os


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


class Settings:
    # Проверка JWT
    issuer = _env("JWT_ISSUER", "http://localhost:8080/realms/reports-realm")
    jwks_uri = _env("JWKS_URI", "http://keycloak:8080/realms/reports-realm/protocol/openid-connect/certs")
    audience = _env("JWT_AUDIENCE", "reports-api")
    required_role = _env("REPORTS_REQUIRED_ROLE", "prothetic_user")

    # ClickHouse (OLAP)
    ch_host = _env("CLICKHOUSE_HOST", "clickhouse")
    ch_port = int(_env("CLICKHOUSE_PORT", "8123"))
    ch_user = _env("CLICKHOUSE_USER", "reports_api")
    ch_password = _env("CLICKHOUSE_PASSWORD")
    ch_database = _env("CLICKHOUSE_DATABASE", "reports")

    # Источник витрины:
    #   airflow - reports.report_daily (строится DAG-ом Airflow, задание 2)
    #   cdc     - reports.report_daily_cdc (строится MaterializedView по данным Debezium, задание 4)
    report_source = _env("REPORT_SOURCE", "cdc")

    # Окно отчета по умолчанию, дней
    default_period_days = int(_env("REPORT_DEFAULT_DAYS", "30"))
    max_period_days = int(_env("REPORT_MAX_DAYS", "366"))

    # S3 (Minio) и CDN
    s3_endpoint = _env("S3_ENDPOINT", "http://minio:9000")
    s3_access_key = _env("S3_ACCESS_KEY")
    s3_secret_key = _env("S3_SECRET_KEY")
    s3_bucket = _env("S3_BUCKET", "reports")
    s3_region = _env("S3_REGION", "us-east-1")
    # Публичный адрес CDN (nginx reverse proxy с кешированием перед Minio)
    cdn_public_url = _env("CDN_PUBLIC_URL", "http://localhost:8081").rstrip("/")
    # Время жизни подписанной ссылки
    link_ttl = int(_env("REPORT_LINK_TTL", "900"))
    # Секрет для непредсказуемого префикса пользователя в S3
    user_key_secret = _env("REPORT_USER_KEY_SECRET").encode()


settings = Settings()
