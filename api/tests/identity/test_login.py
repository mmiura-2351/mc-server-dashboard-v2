"""Unit tests for the Login use case against faked Ports.

Covers the success path (a pair is issued and the refresh row persisted), and
the uniform-failure posture: unknown-user and wrong-password are the same error,
both run the artificial-delay hook, and neither commits a token row.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.application.login import Login
from mc_server_dashboard_api.identity.domain.errors import InvalidCredentialsError
from tests.identity.fakes import (
    FakeClock,
    FakeLoginAttemptStore,
    FakeTokenService,
    FakeUnitOfWork,
    RecordingFailureDelay,
    StubHasher,
    make_brute_force_config,
    make_user,
)

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
_PASSWORD = "Wm7!qz#Lp2vT"
_REFRESH_TTL = dt.timedelta(days=14)


def _login(
    uow: FakeUnitOfWork,
    delay: RecordingFailureDelay,
    attempts: FakeLoginAttemptStore | None = None,
    *,
    now: dt.datetime = _NOW,
) -> Login:
    return Login(
        uow=uow,
        attempts=attempts or FakeLoginAttemptStore(),
        brute_force=make_brute_force_config(),
        hasher=StubHasher(),
        dummy_password_hash="hashed::__dummy__",
        tokens=FakeTokenService(),
        clock=FakeClock(now),
        failure_delay=delay,
        refresh_ttl=_REFRESH_TTL,
    )


async def test_login_success_issues_pair_and_persists_refresh() -> None:
    user = make_user(password=_PASSWORD)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    delay = RecordingFailureDelay()

    result = await _login(uow, delay)(username="alice", password=_PASSWORD)

    assert result.user_id == user.id.value
    pair = result.pair
    assert pair.access_token == f"access::{user.id.value}"
    assert pair.refresh_token == "refresh-secret-1"
    stored = uow.refresh_tokens.by_hash["hash::refresh-secret-1"]
    assert stored.user_id == user.id
    assert stored.issued_at == _NOW
    assert stored.expires_at == _NOW + _REFRESH_TTL
    assert uow.commits == 1
    assert delay.calls == 0


async def test_login_wrong_password_is_uniform_failure() -> None:
    user = make_user(password=_PASSWORD)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    delay = RecordingFailureDelay()

    with pytest.raises(InvalidCredentialsError):
        await _login(uow, delay)(username="alice", password="wrong-password")

    assert uow.refresh_tokens.by_hash == {}
    assert uow.commits == 0
    assert delay.calls == 1


async def test_login_unknown_user_is_same_error_as_wrong_password() -> None:
    uow = FakeUnitOfWork()  # no users seeded
    delay = RecordingFailureDelay()

    with pytest.raises(InvalidCredentialsError):
        await _login(uow, delay)(username="ghost", password=_PASSWORD)

    assert uow.refresh_tokens.by_hash == {}
    assert uow.commits == 0
    assert delay.calls == 1


async def test_login_deactivated_user_is_uniform_failure() -> None:
    # A deactivated account, even with the right password, fails with the same
    # uniform error and the same artificial delay as a wrong password -- no
    # response/timing oracle distinguishing the two (#278). The check runs after
    # the password verify (asserted by the delay being applied), so a deactivated
    # account is indistinguishable from a wrong password to a probing caller.
    user = make_user(password=_PASSWORD, active=False)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    delay = RecordingFailureDelay()

    with pytest.raises(InvalidCredentialsError):
        await _login(uow, delay)(username="alice", password=_PASSWORD)

    assert uow.refresh_tokens.by_hash == {}
    assert uow.commits == 0
    assert delay.calls == 1


async def test_login_deactivated_failure_records_deactivated_reason() -> None:
    # The forensic reason on the attempt row is "deactivated" (never surfaced).
    user = make_user(password=_PASSWORD, active=False)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    attempts = FakeLoginAttemptStore()
    delay = RecordingFailureDelay()

    with pytest.raises(InvalidCredentialsError):
        await _login(uow, delay, attempts)(username="alice", password=_PASSWORD)

    assert [a[3] for a in attempts.attempts] == ["deactivated"]


async def test_failure_records_attempt_with_username_and_ip() -> None:
    uow = FakeUnitOfWork()
    uow.users.seed(make_user(password=_PASSWORD))
    attempts = FakeLoginAttemptStore()

    with pytest.raises(InvalidCredentialsError):
        await _login(uow, RecordingFailureDelay(), attempts)(
            username="alice", password="wrong", ip="203.0.113.7"
        )

    assert attempts.attempts == [
        ("alice", "203.0.113.7", False, "wrong_password", _NOW)
    ]


async def test_success_records_attempt_and_clears_lockout() -> None:
    uow = FakeUnitOfWork()
    user = make_user(password=_PASSWORD)
    uow.users.seed(user)
    attempts = FakeLoginAttemptStore()
    await attempts.lock(
        "alice", locked_until=_NOW - dt.timedelta(seconds=1), lockout_count=2
    )

    await _login(uow, RecordingFailureDelay(), attempts)(
        username="alice", password=_PASSWORD, ip="203.0.113.7"
    )

    assert attempts.attempts == [("alice", "203.0.113.7", True, None, _NOW)]
    assert await attempts.get_lockout("alice") is None


async def test_username_threshold_locks_with_base_duration() -> None:
    uow = FakeUnitOfWork()
    uow.users.seed(make_user(password=_PASSWORD))
    attempts = FakeLoginAttemptStore()
    login = _login(uow, RecordingFailureDelay(), attempts)

    # Five failures (default threshold) inside the window.
    for _ in range(5):
        with pytest.raises(InvalidCredentialsError):
            await login(username="alice", password="wrong", ip="198.51.100.1")

    lockout = await attempts.get_lockout("alice")
    assert lockout is not None
    assert lockout.lockout_count == 1
    # First lockout uses the base duration (15 minutes).
    assert lockout.locked_until == _NOW + dt.timedelta(minutes=15)


async def test_below_threshold_does_not_lock() -> None:
    uow = FakeUnitOfWork()
    uow.users.seed(make_user(password=_PASSWORD))
    attempts = FakeLoginAttemptStore()
    login = _login(uow, RecordingFailureDelay(), attempts)

    for _ in range(4):
        with pytest.raises(InvalidCredentialsError):
            await login(username="alice", password="wrong", ip="198.51.100.1")

    assert await attempts.get_lockout("alice") is None


async def test_active_lockout_rejects_correct_password_uniformly() -> None:
    uow = FakeUnitOfWork()
    uow.users.seed(make_user(password=_PASSWORD))
    attempts = FakeLoginAttemptStore()
    await attempts.lock(
        "alice", locked_until=_NOW + dt.timedelta(minutes=10), lockout_count=1
    )
    delay = RecordingFailureDelay()

    # Even the *correct* password is rejected while locked, and the delay runs —
    # a locked account is indistinguishable from a wrong password.
    with pytest.raises(InvalidCredentialsError):
        await _login(uow, delay, attempts)(
            username="alice", password=_PASSWORD, ip="198.51.100.1"
        )

    assert uow.commits == 0
    assert delay.calls == 1


async def test_expired_lockout_allows_login() -> None:
    uow = FakeUnitOfWork()
    uow.users.seed(make_user(password=_PASSWORD))
    attempts = FakeLoginAttemptStore()
    await attempts.lock(
        "alice", locked_until=_NOW - dt.timedelta(seconds=1), lockout_count=1
    )

    result = await _login(uow, RecordingFailureDelay(), attempts)(
        username="alice", password=_PASSWORD, ip="198.51.100.1"
    )

    assert result.pair.access_token


async def test_repeat_lockout_backoff_doubles() -> None:
    uow = FakeUnitOfWork()
    uow.users.seed(make_user(password=_PASSWORD))
    attempts = FakeLoginAttemptStore()
    # Account previously locked twice; the next lockout is the 3rd event.
    await attempts.lock(
        "alice", locked_until=_NOW - dt.timedelta(seconds=1), lockout_count=2
    )
    login = _login(uow, RecordingFailureDelay(), attempts)

    for _ in range(5):
        with pytest.raises(InvalidCredentialsError):
            await login(username="alice", password="wrong", ip="198.51.100.1")

    lockout = await attempts.get_lockout("alice")
    assert lockout is not None
    assert lockout.lockout_count == 3
    # base * 2**2 = 15min * 4 = 60min.
    assert lockout.locked_until == _NOW + dt.timedelta(minutes=60)


async def test_ip_threshold_blocks_before_password_check() -> None:
    uow = FakeUnitOfWork()
    uow.users.seed(make_user(password=_PASSWORD))
    attempts = FakeLoginAttemptStore()
    # Seed the IP at its threshold (20) from *other* usernames in-window.
    for i in range(20):
        await attempts.record_attempt(
            username=f"victim{i}",
            ip="198.51.100.9",
            success=False,
            failure_reason="wrong_password",
            at=_NOW,
        )
    delay = RecordingFailureDelay()

    # Correct password, but the source IP is over its threshold -> uniform fail.
    with pytest.raises(InvalidCredentialsError):
        await _login(uow, delay, attempts)(
            username="alice", password=_PASSWORD, ip="198.51.100.9"
        )

    assert uow.commits == 0
    assert delay.calls == 1


async def test_disabled_brute_force_skips_store() -> None:
    uow = FakeUnitOfWork()
    uow.users.seed(make_user(password=_PASSWORD))
    attempts = FakeLoginAttemptStore()
    login = Login(
        uow=uow,
        attempts=attempts,
        brute_force=make_brute_force_config(enabled=False),
        hasher=StubHasher(),
        dummy_password_hash="hashed::__dummy__",
        tokens=FakeTokenService(),
        clock=FakeClock(_NOW),
        failure_delay=RecordingFailureDelay(),
        refresh_ttl=_REFRESH_TTL,
    )

    with pytest.raises(InvalidCredentialsError):
        await login(username="alice", password="wrong", ip="198.51.100.1")
    await login(username="alice", password=_PASSWORD, ip="198.51.100.1")

    # No attempts recorded when protection is disabled.
    assert attempts.attempts == []


async def test_failures_across_casing_variants_aggregate_and_lock() -> None:
    # The account exists as "alice"; an attacker spreads failures across casing
    # variants. Because brute-force state keys on the case-folded name, the
    # variants share one counter and the account locks at the threshold.
    uow = FakeUnitOfWork()
    uow.users.seed(make_user(username="alice", password=_PASSWORD))
    attempts = FakeLoginAttemptStore()
    login = _login(uow, RecordingFailureDelay(), attempts)

    variants = ["Alice", "ALICE", "aLiCe", "alice", "AlIcE"]
    for variant in variants:
        with pytest.raises(InvalidCredentialsError):
            await login(username=variant, password="wrong", ip="198.51.100.1")

    # All five aggregate under the folded key, crossing the threshold of 5.
    lockout = await attempts.get_lockout("alice")
    assert lockout is not None
    assert lockout.lockout_count == 1

    # A correct-password attempt under yet another casing is now rejected.
    with pytest.raises(InvalidCredentialsError):
        await login(username="ALicE", password=_PASSWORD, ip="198.51.100.1")


async def test_unknown_user_and_wrong_password_both_verify() -> None:
    # Both failure paths must invoke the hasher so neither is faster (timing
    # enumeration defence). The unknown-user path verifies the dummy hash.
    user = make_user(username="alice", password=_PASSWORD)

    def build(hasher: StubHasher, uow: FakeUnitOfWork) -> Login:
        return Login(
            uow=uow,
            attempts=FakeLoginAttemptStore(),
            brute_force=make_brute_force_config(),
            hasher=hasher,
            dummy_password_hash="hashed::__dummy__",
            tokens=FakeTokenService(),
            clock=FakeClock(_NOW),
            failure_delay=RecordingFailureDelay(),
            refresh_ttl=_REFRESH_TTL,
        )

    wrong_hasher = StubHasher()
    wrong_uow = FakeUnitOfWork()
    wrong_uow.users.seed(user)
    with pytest.raises(InvalidCredentialsError):
        await build(wrong_hasher, wrong_uow)(username="alice", password="wrong")

    unknown_hasher = StubHasher()
    with pytest.raises(InvalidCredentialsError):
        await build(unknown_hasher, FakeUnitOfWork())(
            username="ghost", password=_PASSWORD
        )

    assert wrong_hasher.verify_calls == 1
    assert unknown_hasher.verify_calls == 1
