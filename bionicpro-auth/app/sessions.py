"""Хранилище сессий в Redis.

Ключи:
  session:<sid>      -> JSON сессии (access_token, зашифрованный refresh_token, профиль)
  rotated:<old_sid>  -> <new_sid>, короткоживущая ссылка после ротации session id
  login:<state>      -> JSON с code_verifier/nonce на время PKCE-логина
"""
import json
import secrets
import time
from dataclasses import asdict, dataclass, field

import redis.asyncio as redis

from . import crypto
from .config import settings

_redis = redis.from_url(settings.redis_url, decode_responses=True)

MAX_ROTATION_HOPS = 3


def new_session_id() -> str:
    # 256 бит энтропии
    return secrets.token_urlsafe(32)


@dataclass
class Session:
    sub: str
    username: str
    email: str | None
    name: str | None
    roles: list[str]
    access_token: str
    access_expires_at: float
    refresh_token_enc: str
    refresh_expires_at: float
    id_token: str | None = None
    created_at: float = field(default_factory=time.time)

    # --- refresh_token хранится только в зашифрованном виде ---
    @property
    def refresh_token(self) -> str:
        return crypto.decrypt(self.refresh_token_enc, associated_data=self.sub)

    def set_refresh_token(self, token: str) -> None:
        self.refresh_token_enc = crypto.encrypt(token, associated_data=self.sub)

    def ttl(self) -> int:
        """TTL ключа: idle-таймаут, но не дольше жизни refresh_token."""
        ttl = settings.session_idle_ttl
        if self.refresh_expires_at:
            ttl = min(ttl, int(self.refresh_expires_at - time.time()))
        return max(ttl, 1)

    def public_profile(self) -> dict:
        return {
            "sub": self.sub,
            "username": self.username,
            "email": self.email,
            "name": self.name,
            "roles": self.roles,
            "access_token_expires_in": max(0, int(self.access_expires_at - time.time())),
        }


async def save(sid: str, session: Session) -> None:
    await _redis.set(f"session:{sid}", json.dumps(asdict(session)), ex=session.ttl())


async def load(sid: str) -> tuple[str, Session, bool] | None:
    """Возвращает (актуальный sid, сессия, пришли_ли_по_старому_sid).

    Если sid уже был ротирован, в течение grace-периода он указывает на новый sid.
    """
    via_grace = False
    for _ in range(MAX_ROTATION_HOPS + 1):
        raw = await _redis.get(f"session:{sid}")
        if raw is not None:
            return sid, Session(**json.loads(raw)), via_grace
        target = await _redis.get(f"rotated:{sid}")
        if target is None:
            return None
        sid, via_grace = target, True
    return None


async def rotate(old_sid: str, session: Session) -> str:
    """Перепривязывает токены к новому session id (защита от session fixation)."""
    sid = new_session_id()
    pipe = _redis.pipeline(transaction=True)
    pipe.set(f"session:{sid}", json.dumps(asdict(session)), ex=session.ttl())
    pipe.delete(f"session:{old_sid}")
    pipe.set(f"rotated:{old_sid}", sid, ex=settings.rotation_grace_ttl)
    await pipe.execute()
    return sid


async def delete(sid: str) -> None:
    await _redis.delete(f"session:{sid}")


async def save_login_state(state: str, data: dict, ttl: int = 600) -> None:
    await _redis.set(f"login:{state}", json.dumps(data), ex=ttl)


async def pop_login_state(state: str) -> dict | None:
    raw = await _redis.getdel(f"login:{state}")
    return json.loads(raw) if raw else None


async def ping() -> bool:
    return await _redis.ping()
