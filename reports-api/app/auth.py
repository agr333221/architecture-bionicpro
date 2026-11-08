"""Проверка access_token (JWT от Keycloak) и ролевая модель доступа к отчетам."""
from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException

from .config import settings

_jwks = jwt.PyJWKClient(settings.jwks_uri, cache_keys=True, lifespan=300)


@dataclass
class Principal:
    sub: str
    username: str
    email: str
    roles: set[str]


def get_principal(authorization: str | None = Header(default=None)) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        key = _jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            issuer=settings.issuer,
            audience=settings.audience,
            leeway=10,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"invalid token: {exc}") from exc

    roles = set(claims.get("realm_access", {}).get("roles", []))
    if settings.required_role not in roles:
        raise HTTPException(403, f"role '{settings.required_role}' is required")

    email = claims.get("email")
    if not email:
        raise HTTPException(403, "token has no email claim, cannot match CRM customer")

    return Principal(
        sub=claims["sub"],
        username=claims.get("preferred_username", ""),
        email=email.lower(),
        roles=roles,
    )
