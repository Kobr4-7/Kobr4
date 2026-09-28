"""Mots de passe, sessions, double authentification, limitation des tentatives."""

import hashlib
import secrets
import time
from collections import defaultdict, deque

import pyotp
import segno
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 12


def password_problem(password: str, email: str = "") -> str | None:
    """Motif de refus d'un mot de passe, ou None s'il convient."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"au moins {MIN_PASSWORD_LENGTH} caractères"
    if len(set(password)) < 6:
        return "trop peu de caractères différents"
    local = email.split("@")[0].lower()
    if local and len(local) >= 4 and local in password.lower():
        return "ne doit pas contenir ton adresse email"
    return None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# Sessions : le navigateur garde le jeton, la base n'en garde que l'empreinte.


def new_session_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, token_digest(token)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# Double authentification (TOTP, applications type Google Authenticator, 1Password…)


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="Kobr4 FX")


def totp_qr_svg(uri: str) -> str:
    return str(segno.make(uri, error="m").svg_inline(scale=5, dark="#15202B", light="#FFFFFF"))


def verify_totp(secret: str, code: str) -> bool:
    code = code.replace(" ", "")
    return code.isdigit() and pyotp.TOTP(secret).verify(code, valid_window=1)


def new_recovery_codes(n: int = 8) -> tuple[list[str], list[str]]:
    """Codes de secours lisibles (à montrer une fois) et leurs empreintes (à stocker)."""
    codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(n)]
    return codes, [token_digest(c) for c in codes]


class RateLimiter:
    """Au plus `limit` tentatives par clé (ex. adresse IP) sur `window` secondes."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True
