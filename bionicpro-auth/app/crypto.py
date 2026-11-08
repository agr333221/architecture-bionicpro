"""Шифрование refresh_token перед сохранением в распределенный кеш (AES-256-GCM)."""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings

_aead = AESGCM(settings.token_enc_key)


def encrypt(plaintext: str, associated_data: str) -> str:
    nonce = os.urandom(12)
    ct = _aead.encrypt(nonce, plaintext.encode("utf-8"), associated_data.encode("utf-8"))
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt(token: str, associated_data: str) -> str:
    raw = base64.b64decode(token)
    nonce, ct = raw[:12], raw[12:]
    return _aead.decrypt(nonce, ct, associated_data.encode("utf-8")).decode("utf-8")
