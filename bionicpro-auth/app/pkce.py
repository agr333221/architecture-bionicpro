"""PKCE (RFC 7636): генерация code_verifier и code_challenge (метод S256)."""
import base64
import hashlib
import secrets


def generate_code_verifier() -> str:
    # 64 случайных байта -> 86 символов base64url (допустимо 43..128 символов)
    return secrets.token_urlsafe(64)


def code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
