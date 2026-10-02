from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .store import SessionRecord, ValidationError, WebStore


SESSION_COOKIE = "quant_web_session"
LOGIN_CSRF_COOKIE = "quant_web_login_csrf"


class AuthenticationError(RuntimeError):
    pass


class LoginRateLimited(AuthenticationError):
    pass


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_token: str
    expires_at: str


class LocalAuthService:
    def __init__(self, store: WebStore, *, session_hours: int = 12):
        self.store = store
        self.session_hours = session_hours
        self.password_hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
        )

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def validate_password(password: str) -> None:
        if len(password) < 12:
            raise ValidationError("Password must be at least 12 characters")
        if len(password) > 256:
            raise ValidationError("Password is too long")

    def bootstrap_user(self, username: str, password: str, *, replace: bool = False) -> None:
        self.validate_password(password)
        password_hash = self.password_hasher.hash(password)
        self.store.upsert_user(username, password_hash, replace=replace)

    def authenticate(
        self, username: str, password: str, *, remote_key: str
    ) -> IssuedSession:
        if self.store.login_is_limited(remote_key):
            raise LoginRateLimited("Too many failed login attempts; try again later")
        encoded = self.store.password_hash(username)
        verified = False
        if encoded:
            try:
                verified = self.password_hasher.verify(encoded, password)
            except (VerifyMismatchError, InvalidHashError):
                verified = False
        self.store.record_login_attempt(remote_key, bool(verified))
        if not verified:
            raise AuthenticationError("Invalid username or password")
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            hours=self.session_hours
        )
        expires_at = expires.isoformat()
        self.store.create_session(
            token_hash=self.token_hash(token),
            username=username.strip().lower(),
            csrf_token=csrf_token,
            expires_at=expires_at,
        )
        return IssuedSession(token, csrf_token, expires_at)

    def session(self, token: str | None) -> SessionRecord | None:
        if not token or len(token) > 200:
            return None
        return self.store.get_session(self.token_hash(token))

    def require_csrf(self, session: SessionRecord, supplied: str | None) -> None:
        if not supplied or not hmac.compare_digest(session.csrf_token, supplied):
            raise AuthenticationError("Invalid CSRF token")

    def logout(self, token: str | None) -> None:
        if token:
            self.store.revoke_session(self.token_hash(token))
