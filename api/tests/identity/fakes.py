"""In-memory fakes for the identity Ports used by the auth use-case tests.

Keeps the use cases under test against fakes (no database, no JWT lib), per
TESTING.md Section 4. The fake UnitOfWork shares its repositories across nested
``async with`` blocks and tracks commits so tests can assert atomicity.
"""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.identity.domain.brute_force import BruteForceConfig
from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.entities import RefreshToken, User
from mc_server_dashboard_api.identity.domain.login_attempt_store import (
    Lockout,
    LoginAttemptStore,
)
from mc_server_dashboard_api.identity.domain.login_failure_delay import (
    LoginFailureDelay,
)
from mc_server_dashboard_api.identity.domain.password_hasher import PasswordHasher
from mc_server_dashboard_api.identity.domain.repositories import (
    RefreshTokenRepository,
    UserRepository,
)
from mc_server_dashboard_api.identity.domain.sleeper import Sleeper
from mc_server_dashboard_api.identity.domain.token_service import (
    IssuedRefreshToken,
    TokenService,
)
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)


class FakeClock(Clock):
    def __init__(self, now: dt.datetime) -> None:
        self._now = now

    def set(self, now: dt.datetime) -> None:
        self._now = now

    def now(self) -> dt.datetime:
        return self._now


class FakeUserRepository(UserRepository):
    def __init__(self) -> None:
        self.by_id: dict[UserId, User] = {}

    def seed(self, user: User) -> None:
        self.by_id[user.id] = user

    async def add(self, user: User) -> None:
        self.by_id[user.id] = user

    async def get_by_id(self, user_id: UserId) -> User | None:
        return self.by_id.get(user_id)

    async def get_by_username(self, username: Username) -> User | None:
        for user in self.by_id.values():
            if user.username == username:
                return user
        return None

    async def get_by_email(self, email: EmailAddress) -> User | None:
        for user in self.by_id.values():
            if user.email == email:
                return user
        return None


class FakeRefreshTokenRepository(RefreshTokenRepository):
    def __init__(self) -> None:
        self.by_hash: dict[str, RefreshToken] = {}

    def seed(self, token: RefreshToken) -> None:
        self.by_hash[token.token_hash] = token

    async def add(self, token: RefreshToken) -> None:
        self.by_hash[token.token_hash] = token

    async def get_by_token_hash(self, token_hash: str) -> RefreshToken | None:
        return self.by_hash.get(token_hash)

    async def revoke(self, token_hash: str, *, revoked_at: dt.datetime) -> None:
        existing = self.by_hash.get(token_hash)
        if existing is not None:
            self.by_hash[token_hash] = _with_revoked(existing, revoked_at)

    async def revoke_all_for_user(
        self, user_id: UserId, *, revoked_at: dt.datetime
    ) -> None:
        for token_hash, token in list(self.by_hash.items()):
            if token.user_id == user_id and token.revoked_at is None:
                self.by_hash[token_hash] = _with_revoked(token, revoked_at)


def _with_revoked(token: RefreshToken, revoked_at: dt.datetime) -> RefreshToken:
    return RefreshToken(
        id=token.id,
        user_id=token.user_id,
        token_hash=token.token_hash,
        issued_at=token.issued_at,
        expires_at=token.expires_at,
        revoked_at=revoked_at,
    )


class FakeUnitOfWork(UnitOfWork):
    # Narrow the Port-declared attribute types to the concrete fakes so tests can
    # reach their inspection helpers (``seed`` / ``by_hash``) without casts.
    users: FakeUserRepository
    refresh_tokens: FakeRefreshTokenRepository

    def __init__(
        self,
        users: FakeUserRepository | None = None,
        refresh_tokens: FakeRefreshTokenRepository | None = None,
    ) -> None:
        self.users = users or FakeUserRepository()
        self.refresh_tokens = refresh_tokens or FakeRefreshTokenRepository()
        self.commits = 0

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class StubHasher(PasswordHasher):
    """Treats a hash of the form ``hashed::<plaintext>`` as the stored hash.

    Counts ``verify`` calls so tests can assert the unknown-user and
    wrong-password paths both run a verify (timing-enumeration defence).
    """

    def __init__(self) -> None:
        self.verify_calls = 0

    def hash(self, plaintext: str) -> str:
        return f"hashed::{plaintext}"

    def verify(self, plaintext: str, password_hash: str) -> bool:
        self.verify_calls += 1
        return password_hash == f"hashed::{plaintext}"


class FakeTokenService(TokenService):
    """Deterministic token service: access token == ``access::<uuid>``."""

    def __init__(self) -> None:
        self._counter = 0

    def issue_access_token(self, user_id: UserId) -> str:
        return f"access::{user_id.value}"

    def verify_access_token(self, token: str) -> UserId:
        import uuid

        from mc_server_dashboard_api.identity.domain.errors import (
            InvalidAccessTokenError,
        )

        prefix = "access::"
        if not token.startswith(prefix):
            raise InvalidAccessTokenError
        try:
            return UserId(uuid.UUID(token[len(prefix) :]))
        except ValueError as exc:
            raise InvalidAccessTokenError from exc

    def issue_refresh_token(self) -> IssuedRefreshToken:
        self._counter += 1
        secret = f"refresh-secret-{self._counter}"
        return IssuedRefreshToken(
            secret=secret, token_hash=self.hash_refresh_token(secret)
        )

    def hash_refresh_token(self, secret: str) -> str:
        return f"hash::{secret}"


class RecordingFailureDelay(LoginFailureDelay):
    def __init__(self) -> None:
        self.calls = 0

    async def apply(self) -> None:
        self.calls += 1


class RecordingSleeper(Sleeper):
    """Records requested sleep durations instead of really sleeping (TESTING 4)."""

    def __init__(self) -> None:
        self.sleeps: list[dt.timedelta] = []

    async def sleep(self, duration: dt.timedelta) -> None:
        self.sleeps.append(duration)


class FakeLoginAttemptStore(LoginAttemptStore):
    """In-memory :class:`LoginAttemptStore`: a list of attempts + lockout map."""

    def __init__(self) -> None:
        # (username, ip, success, failure_reason, created_at)
        self.attempts: list[tuple[str, str | None, bool, str | None, dt.datetime]] = []
        self.lockouts: dict[str, Lockout] = {}

    async def record_attempt(
        self,
        *,
        username: str,
        ip: str | None,
        success: bool,
        failure_reason: str | None,
        at: dt.datetime,
    ) -> None:
        self.attempts.append((username, ip, success, failure_reason, at))

    async def count_username_failures(
        self, username: str, *, since: dt.datetime
    ) -> int:
        return sum(
            1
            for (name, _ip, success, _reason, at) in self.attempts
            if name == username and not success and at >= since
        )

    async def count_ip_failures(self, ip: str, *, since: dt.datetime) -> int:
        return sum(
            1
            for (_name, attempt_ip, success, _reason, at) in self.attempts
            if attempt_ip == ip and not success and at >= since
        )

    async def get_lockout(self, username: str) -> Lockout | None:
        return self.lockouts.get(username)

    async def lock(
        self, username: str, *, locked_until: dt.datetime, lockout_count: int
    ) -> None:
        self.lockouts[username] = Lockout(
            locked_until=locked_until, lockout_count=lockout_count
        )

    async def clear_lockout(self, username: str) -> None:
        self.lockouts.pop(username, None)

    async def prune_attempts(self, *, older_than: dt.datetime) -> None:
        self.attempts = [a for a in self.attempts if a[4] >= older_than]


def make_brute_force_config(
    *,
    enabled: bool = True,
    username_threshold: int = 5,
    username_window: dt.timedelta = dt.timedelta(minutes=15),
    ip_threshold: int = 20,
    ip_window: dt.timedelta = dt.timedelta(minutes=5),
    lockout_base: dt.timedelta = dt.timedelta(minutes=15),
    lockout_max: dt.timedelta = dt.timedelta(days=1),
    delay: dt.timedelta = dt.timedelta(milliseconds=200),
) -> BruteForceConfig:
    return BruteForceConfig(
        enabled=enabled,
        username_threshold=username_threshold,
        username_window=username_window,
        ip_threshold=ip_threshold,
        ip_window=ip_window,
        lockout_base=lockout_base,
        lockout_max=lockout_max,
        delay=delay,
    )


def make_user(
    *,
    username: str = "alice",
    email: str = "alice@example.com",
    password: str = "Wm7!qz#Lp2vT",
    now: dt.datetime | None = None,
) -> User:
    moment = now or dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
    return User(
        id=UserId.new(),
        username=Username(username),
        email=EmailAddress(email),
        password_hash=f"hashed::{password}",
        created_at=moment,
        updated_at=moment,
    )
