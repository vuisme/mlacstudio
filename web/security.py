"""Authentication, CSRF, throttling, and request-origin helpers."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PBKDF2_ITERATIONS = 600_000
SESSION_SECONDS = 24 * 60 * 60
THROTTLE_WINDOW = 15 * 60
THROTTLE_LIMIT = 5


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def password_hash(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def valid_password(password: str) -> None:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    if len(password) > 1024:
        raise ValueError("password is too long")


def cookie_value(header: str | None, name: str) -> str | None:
    if not header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(header)
    except Exception:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()


def session_cookie(token: str, max_age: int = SESSION_SECONDS) -> str:
    return f"qis_session={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"


def preauth_cookie(token: str, max_age: int = 600) -> str:
    return f"qis_preauth={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"


def clear_session_cookie() -> str:
    return "qis_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"


def host_allowed(hostport: str) -> bool:
    if not hostport:
        return False
    host = hostport.rsplit(":", 1)[0] if hostport.count(":") == 1 else hostport
    host = host.strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def peer_allowed(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def same_origin_allowed(method: str, headers: Any) -> bool:
    if method in {"GET", "HEAD", "OPTIONS"}:
        return True
    host = headers.get("Host", "")
    origin = headers.get("Origin")
    if origin:
        parsed = urlparse(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host.lower()
    return headers.get("Sec-Fetch-Site", "") != "cross-site"


@dataclass(frozen=True)
class AuthSession:
    username: str
    csrf: str


class AuthStore:
    def __init__(self, database: Path, clock=time.time) -> None:
        self.database = database
        self.clock = clock

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database, timeout=10, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    def is_configured(self) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM admin LIMIT 1").fetchone() is not None

    def create_admin(self, username: str, password: str) -> None:
        username = username.strip()
        if not username or len(username) > 80:
            raise ValueError("username is required and must be at most 80 characters")
        valid_password(password)
        salt = secrets.token_bytes(32)
        digest = password_hash(password, salt)
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO admin (id, username, salt, password_hash, iterations, created_at) VALUES (1, ?, ?, ?, ?, ?)",
                    (username, salt, digest, PBKDF2_ITERATIONS, int(self.clock())),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("admin setup is already complete") from exc

    def throttled(self, address: str) -> tuple[bool, int]:
        cutoff = int(self.clock()) - THROTTLE_WINDOW
        with self._connect() as conn:
            conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
            row = conn.execute(
                "SELECT COUNT(*) AS failures, MIN(attempted_at) AS oldest FROM login_attempts "
                "WHERE address = ? AND success = 0",
                (address,),
            ).fetchone()
        failures = int(row["failures"])
        if failures < THROTTLE_LIMIT:
            return False, 0
        retry = max(1, THROTTLE_WINDOW - (int(self.clock()) - int(row["oldest"])))
        return True, retry

    def authenticate(self, address: str, username: str, password: str) -> str | None:
        blocked, _ = self.throttled(address)
        if blocked:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM admin WHERE id = 1").fetchone()
        ok = False
        if row is not None:
            candidate = password_hash(password, bytes(row["salt"]), int(row["iterations"]))
            ok = hmac.compare_digest(candidate, bytes(row["password_hash"])) and hmac.compare_digest(
                username.encode("utf-8"), str(row["username"]).encode("utf-8")
            )
        now = int(self.clock())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO login_attempts (address, attempted_at, success) VALUES (?, ?, ?)",
                (address, now, int(ok)),
            )
            if ok:
                conn.execute("DELETE FROM login_attempts WHERE address = ?", (address,))
        return str(row["username"]) if ok and row is not None else None

    def create_session(self, username: str) -> tuple[str, str]:
        raw = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        now = int(self.clock())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO auth_sessions (token_hash, username, csrf, created_at, last_seen, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (token_hash(raw), username, csrf, now, now, now + SESSION_SECONDS),
            )
        return raw, csrf

    def get_session(self, raw: str | None) -> AuthSession | None:
        if not raw:
            return None
        now = int(self.clock())
        digest = token_hash(raw)
        with self._connect() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))
            row = conn.execute(
                "SELECT username, csrf FROM auth_sessions WHERE token_hash = ? AND expires_at > ?",
                (digest, now),
            ).fetchone()
            if row:
                conn.execute("UPDATE auth_sessions SET last_seen = ? WHERE token_hash = ?", (now, digest))
        return AuthSession(str(row["username"]), str(row["csrf"])) if row else None

    def delete_session(self, raw: str | None) -> None:
        if not raw:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash(raw),))


class PreAuthTokens:
    """Short-lived in-memory CSRF tokens for setup and login."""

    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self.tokens: dict[str, tuple[str, float]] = {}
        self.lock = threading.Lock()

    def issue(self) -> tuple[str, str]:
        cookie = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self.lock:
            self.tokens[token_hash(cookie)] = (csrf, self.clock() + 600)
            self._prune()
        return cookie, csrf

    def verify(self, cookie: str | None, csrf: str | None) -> bool:
        if not cookie or not csrf:
            return False
        with self.lock:
            self._prune()
            found = self.tokens.get(token_hash(cookie))
        return bool(found and hmac.compare_digest(found[0], csrf))

    def consume(self, cookie: str | None) -> None:
        if cookie:
            with self.lock:
                self.tokens.pop(token_hash(cookie), None)

    def _prune(self) -> None:
        now = self.clock()
        for key, (_, expires) in list(self.tokens.items()):
            if expires <= now:
                self.tokens.pop(key, None)
