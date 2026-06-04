"""Unit tests for the JWT TokenService adapter against a faked Clock.

Exercises access-token round-trip, expiry, tamper/wrong-key rejection, and the
opaque-refresh-token contract (the secret is not its hash, the hash is stable).
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.adapters.token_service import JwtTokenService
from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.errors import InvalidAccessTokenError
from mc_server_dashboard_api.identity.domain.value_objects import UserId

_USER = UserId.new()


class _FakeClock(Clock):
    def __init__(self, now: dt.datetime) -> None:
        self._now = now

    def set(self, now: dt.datetime) -> None:
        self._now = now

    def now(self) -> dt.datetime:
        return self._now


# A 32-byte HS256 key (RFC 7518 minimum) to keep tests free of insecure-key
# warnings; the value itself is irrelevant to what is under test.
_KEY = "0123456789abcdef0123456789abcdef"


def _service(
    clock: Clock, *, key: str = _KEY, access_seconds: int = 900
) -> JwtTokenService:
    return JwtTokenService(
        signing_key=key,
        algorithm="HS256",
        access_ttl=dt.timedelta(seconds=access_seconds),
        clock=clock,
    )


def test_access_token_round_trips_subject() -> None:
    clock = _FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc))
    svc = _service(clock)
    token = svc.issue_access_token(_USER)
    assert svc.verify_access_token(token) == _USER


def test_access_token_rejected_after_expiry() -> None:
    clock = _FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc))
    svc = _service(clock, access_seconds=900)
    token = svc.issue_access_token(_USER)
    # Advance past the 15-minute TTL; verification must fail.
    clock.set(dt.datetime(2026, 6, 4, 0, 16, tzinfo=dt.timezone.utc))
    with pytest.raises(InvalidAccessTokenError):
        svc.verify_access_token(token)


def test_access_token_valid_just_before_expiry() -> None:
    clock = _FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc))
    svc = _service(clock, access_seconds=900)
    token = svc.issue_access_token(_USER)
    clock.set(dt.datetime(2026, 6, 4, 0, 14, tzinfo=dt.timezone.utc))
    assert svc.verify_access_token(token) == _USER


def test_token_signed_with_other_key_is_rejected() -> None:
    clock = _FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc))
    token = _service(clock, key="a" * 32).issue_access_token(_USER)
    with pytest.raises(InvalidAccessTokenError):
        _service(clock, key="b" * 32).verify_access_token(token)


def test_garbage_token_is_rejected() -> None:
    clock = _FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc))
    with pytest.raises(InvalidAccessTokenError):
        _service(clock).verify_access_token("not-a-jwt")


def test_refresh_secret_is_opaque_not_its_hash() -> None:
    clock = _FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc))
    svc = _service(clock)
    issued = svc.issue_refresh_token()
    assert issued.secret != issued.token_hash
    # The stored hash is reproducible from the presented secret.
    assert svc.hash_refresh_token(issued.secret) == issued.token_hash


def test_refresh_secrets_are_unique() -> None:
    clock = _FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc))
    svc = _service(clock)
    assert svc.issue_refresh_token().secret != svc.issue_refresh_token().secret
