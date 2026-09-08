from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from typing import Any, Iterator, Mapping

from .business_store import ensure_database_ready
from .database import DatabaseSession, DatabaseSettings, connection_pool


ROLES = ("read_only", "researcher", "admin")
ROLE_RANK = {role: index for index, role in enumerate(ROLES, start=1)}
SESSION_COOKIE_NAME = "hawknetic_research_session"
CSRF_COOKIE_NAME = "hawknetic_research_csrf"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _audit_hash(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def role_allows(actual_role: str, required_role: str) -> bool:
    return ROLE_RANK.get(actual_role, 0) >= ROLE_RANK.get(required_role, 99)


@dataclass(frozen=True)
class AuthPrincipal:
    username: str
    role: str
    auth_method: str
    user_id: int | None = None
    csrf_token: str | None = None
    session_expires_at: str | None = None


class LocalAuthStore:
    """PostgreSQL-backed private authentication and audit store."""

    def __init__(self, settings: DatabaseSettings | None = None) -> None:
        self.settings = ensure_database_ready(settings)

    @contextmanager
    def connection(self) -> Iterator[DatabaseSession]:
        with connection_pool(self.settings).connection() as connection:
            yield connection

    @staticmethod
    def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
        if len(password) < 12:
            raise ValueError("password_must_be_at_least_12_characters")
        password_salt = salt or secrets.token_bytes(16)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=password_salt,
            n=2**14,
            r=8,
            p=1,
            dklen=32,
        )
        return digest.hex(), password_salt.hex()

    @staticmethod
    def verify_password(password: str, expected_hash: str, salt_hex: str) -> bool:
        try:
            actual_hash, _ = LocalAuthStore.hash_password(password, bytes.fromhex(salt_hex))
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(actual_hash, expected_hash)

    def create_user(self, username: str, password: str, *, role: str) -> dict[str, Any]:
        normalized_username = str(username).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", normalized_username):
            raise ValueError("invalid_username")
        if role not in ROLES:
            raise ValueError("invalid_role")
        password_hash, password_salt = self.hash_password(password)
        now = utc_iso()
        with self.connection() as connection:
            try:
                row = connection.execute(
                    """
                    INSERT INTO auth.app_users
                        (username, password_hash, password_salt, password_algorithm,
                         role, is_disabled, failed_login_count, created_at, updated_at)
                    VALUES (%s, %s, %s, 'scrypt', %s, FALSE, 0, %s, %s)
                    RETURNING id
                    """,
                    (normalized_username, password_hash, password_salt, role, now, now),
                ).fetchone()
            except Exception as exc:
                if "unique" in str(exc).lower():
                    raise ValueError("username_already_exists") from exc
                raise
        if row is None:  # pragma: no cover - PostgreSQL RETURNING guarantees a row
            raise RuntimeError("user_create_did_not_return_identifier")
        return {"user_id": int(row["id"]), "username": normalized_username, "role": role, "created_at": now}

    def set_disabled(self, username: str, *, disabled: bool) -> bool:
        normalized_username = str(username).strip().lower()
        now = utc_iso()
        with self.connection() as connection:
            row = connection.execute(
                """
                UPDATE auth.app_users
                SET is_disabled = %s, updated_at = %s
                WHERE username = %s
                RETURNING id
                """,
                (disabled, now, normalized_username),
            ).fetchone()
            if row is None:
                return False
            if disabled:
                connection.execute(
                    """
                    UPDATE auth.app_sessions
                    SET revoked_at = %s
                    WHERE user_id = %s AND revoked_at IS NULL
                    """,
                    (now, row["id"]),
                )
        return True

    def authenticate_password(
        self,
        username: str,
        password: str,
        *,
        remote_address: str | None = None,
        user_agent: str | None = None,
        maximum_failures: int = 5,
        lock_minutes: int = 15,
    ) -> AuthPrincipal | None:
        normalized_username = str(username).strip().lower()
        now = utc_now()
        failure_reason: str | None = None
        principal: AuthPrincipal | None = None
        with self.connection() as connection:
            user = connection.execute(
                "SELECT * FROM auth.app_users WHERE username = %s FOR UPDATE",
                (normalized_username,),
            ).fetchone()
            if user is None:
                failure_reason = "invalid_credentials"
            elif bool(user["is_disabled"]):
                failure_reason = "account_disabled"
            elif (locked_until := _parse_timestamp(user["locked_until"])) and locked_until > now:
                failure_reason = "account_locked"
            elif not self.verify_password(password, user["password_hash"], user["password_salt"]):
                failure_reason = "invalid_credentials"
                lock_until = utc_iso(now + timedelta(minutes=lock_minutes))
                connection.execute(
                    """
                    UPDATE auth.app_users
                    SET failed_login_count = failed_login_count + 1,
                        locked_until = CASE
                            WHEN failed_login_count + 1 >= %s THEN %s
                            ELSE locked_until
                        END,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (max(1, int(maximum_failures)), lock_until, utc_iso(now), user["id"]),
                )
            else:
                connection.execute(
                    """
                    UPDATE auth.app_users
                    SET failed_login_count = 0, locked_until = NULL, updated_at = %s
                    WHERE id = %s
                    """,
                    (utc_iso(now), user["id"]),
                )
                principal = AuthPrincipal(
                    username=str(user["username"]),
                    role=str(user["role"]),
                    auth_method="password",
                    user_id=int(user["id"]),
                )
            connection.execute(
                """
                INSERT INTO auth.login_audit
                    (username, attempted_at, successful, failure_reason,
                     remote_address_hash, user_agent_hash)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    normalized_username,
                    utc_iso(now),
                    principal is not None,
                    failure_reason,
                    _audit_hash(remote_address),
                    _audit_hash(user_agent),
                ),
            )
        return principal

    def create_session(self, principal: AuthPrincipal, *, duration_minutes: int = 480) -> tuple[str, AuthPrincipal]:
        if principal.user_id is None:
            raise ValueError("session_requires_local_user")
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        now = utc_now()
        expires = now + timedelta(minutes=max(5, duration_minutes))
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO auth.app_sessions
                    (session_id_hash, user_id, csrf_token_hash, created_at,
                     expires_at, last_seen_at, revoked_at)
                VALUES (%s, %s, %s, %s, %s, %s, NULL)
                """,
                (
                    _token_hash(session_token),
                    principal.user_id,
                    _token_hash(csrf_token),
                    utc_iso(now),
                    utc_iso(expires),
                    utc_iso(now),
                ),
            )
        return session_token, AuthPrincipal(
            username=principal.username,
            role=principal.role,
            auth_method="session",
            user_id=principal.user_id,
            csrf_token=csrf_token,
            session_expires_at=utc_iso(expires),
        )

    def resolve_session(self, session_token: str) -> AuthPrincipal | None:
        if not session_token:
            return None
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT s.*, u.username, u.role, u.is_disabled
                FROM auth.app_sessions AS s
                JOIN auth.app_users AS u ON u.id = s.user_id
                WHERE s.session_id_hash = %s
                """,
                (_token_hash(session_token),),
            ).fetchone()
            if (
                row is None
                or row["revoked_at"] is not None
                or bool(row["is_disabled"])
                or (_parse_timestamp(row["expires_at"]) or now) <= now
            ):
                return None
            connection.execute(
                "UPDATE auth.app_sessions SET last_seen_at = %s WHERE session_id_hash = %s",
                (utc_iso(now), _token_hash(session_token)),
            )
            return AuthPrincipal(
                username=str(row["username"]),
                role=str(row["role"]),
                auth_method="session",
                user_id=int(row["user_id"]),
                csrf_token=None,
                session_expires_at=str(row["expires_at"]),
            )

    def rotate_csrf(self, session_token: str) -> str | None:
        """Issue a replacement CSRF token for a live session.

        Only the token's hash is stored, so a session that outlives the copy
        the browser was handed at sign-in cannot have its original recovered.
        Minting a new one lets an authenticated caller recover without being
        forced to sign in again; a session that is missing, expired, or revoked
        gets nothing.
        """
        if not session_token:
            return None
        csrf_token = secrets.token_urlsafe(24)
        with self.connection() as connection:
            row = connection.execute(
                """
                UPDATE auth.app_sessions
                SET csrf_token_hash = %s
                WHERE session_id_hash = %s
                  AND revoked_at IS NULL
                  AND expires_at > %s
                RETURNING session_id_hash
                """,
                (_token_hash(csrf_token), _token_hash(session_token), utc_iso()),
            ).fetchone()
        return csrf_token if row else None

    def validate_csrf(self, session_token: str, csrf_token: str | None) -> bool:
        if not session_token or not csrf_token:
            return False
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT csrf_token_hash, expires_at, revoked_at
                FROM auth.app_sessions
                WHERE session_id_hash = %s
                """,
                (_token_hash(session_token),),
            ).fetchone()
        return bool(
            row
            and row["revoked_at"] is None
            and (_parse_timestamp(row["expires_at"]) or utc_now()) > utc_now()
            and hmac.compare_digest(str(row["csrf_token_hash"]), _token_hash(csrf_token))
        )

    def revoke_session(self, session_token: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                """
                UPDATE auth.app_sessions
                SET revoked_at = %s
                WHERE session_id_hash = %s AND revoked_at IS NULL
                RETURNING session_id_hash
                """,
                (utc_iso(), _token_hash(session_token)),
            ).fetchone()
        return row is not None


def _cookie_value(cookie_header: str | None, name: str) -> str | None:
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None
    morsel = cookie.get(name)
    return morsel.value if morsel else None


def session_token_from_cookie(cookie_header: str | None) -> str | None:
    return _cookie_value(cookie_header, SESSION_COOKIE_NAME)


def csrf_token_from_cookie(cookie_header: str | None) -> str | None:
    return _cookie_value(cookie_header, CSRF_COOKIE_NAME)


def user_auth_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return str(values.get("DASHBOARD_USER_AUTH_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}
