"""Unit tests for the JWT TokenService adapter against a faked Clock.

Exercises access-token round-trip, expiry, tamper/wrong-key rejection, the
opaque-refresh-token contract (the secret is not its hash, the hash is stable),
and the download-grant kind (issue #2313) including the separation guard that
keeps grants and access tokens non-interchangeable in both directions.
"""

from __future__ import annotations

import datetime as dt

import jwt
import pytest

from mc_server_dashboard_api.identity.adapters.token_service import JwtTokenService
from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidAccessTokenError,
    InvalidDownloadGrantError,
)
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
    clock: Clock,
    *,
    key: str = _KEY,
    access_seconds: int = 900,
    grant_seconds: int = 30,
) -> JwtTokenService:
    return JwtTokenService(
        signing_key=key,
        algorithm="HS256",
        access_ttl=dt.timedelta(seconds=access_seconds),
        download_grant_ttl=dt.timedelta(seconds=grant_seconds),
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


# --- download grants (issue #2313) -----------------------------------------

_RESOURCE = "backup-download:c:s:b"


def test_download_grant_round_trips_subject_for_its_resource() -> None:
    clock = _FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc))
    svc = _service(clock)
    issued = svc.issue_download_grant(_USER, _RESOURCE)
    assert svc.verify_download_grant(issued.token, _RESOURCE) == _USER


def test_download_grant_reports_its_expiry() -> None:
    now = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
    svc = _service(_FakeClock(now), grant_seconds=30)
    issued = svc.issue_download_grant(_USER, _RESOURCE)
    assert issued.expires_at == now + dt.timedelta(seconds=30)


def test_download_grant_expiry_matches_the_enforced_exp_claim() -> None:
    """Sub-second issue time must not push the advertised deadline past ``exp``.

    ``exp`` is a whole-second claim, so a grant minted mid-second stops verifying
    before an advertised deadline carrying the fraction (issue #2324).
    """
    now = dt.datetime(2026, 6, 4, 0, 0, 0, 750_000, tzinfo=dt.timezone.utc)
    clock = _FakeClock(now)
    svc = _service(clock, grant_seconds=30)
    issued = svc.issue_download_grant(_USER, _RESOURCE)
    clock.set(issued.expires_at - dt.timedelta(milliseconds=1))
    assert svc.verify_download_grant(issued.token, _RESOURCE) == _USER


def test_download_grant_rejected_after_expiry() -> None:
    clock = _FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc))
    svc = _service(clock, grant_seconds=30)
    issued = svc.issue_download_grant(_USER, _RESOURCE)
    clock.set(dt.datetime(2026, 6, 4, 0, 0, 30, tzinfo=dt.timezone.utc))
    with pytest.raises(InvalidDownloadGrantError):
        svc.verify_download_grant(issued.token, _RESOURCE)


def test_download_grant_valid_just_before_expiry() -> None:
    clock = _FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc))
    svc = _service(clock, grant_seconds=30)
    issued = svc.issue_download_grant(_USER, _RESOURCE)
    clock.set(dt.datetime(2026, 6, 4, 0, 0, 29, tzinfo=dt.timezone.utc))
    assert svc.verify_download_grant(issued.token, _RESOURCE) == _USER


def test_download_grant_rejected_for_another_resource() -> None:
    clock = _FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc))
    svc = _service(clock)
    issued = svc.issue_download_grant(_USER, _RESOURCE)
    with pytest.raises(InvalidDownloadGrantError):
        svc.verify_download_grant(issued.token, "backup-download:c:s:other")


def test_download_grant_with_tampered_signature_is_rejected() -> None:
    clock = _FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc))
    issued = _service(clock, key="a" * 32).issue_download_grant(_USER, _RESOURCE)
    with pytest.raises(InvalidDownloadGrantError):
        _service(clock, key="b" * 32).verify_download_grant(issued.token, _RESOURCE)


def test_token_without_res_claim_is_not_a_download_grant() -> None:
    # A token carrying the download purpose but no resource binds to nothing; it
    # must never verify against an expected resource.
    now = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
    token = jwt.encode(
        {
            "sub": str(_USER.value),
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(seconds=30)).timestamp()),
            "purpose": "download",
        },
        _KEY,
        algorithm="HS256",
    )
    with pytest.raises(InvalidDownloadGrantError):
        _service(_FakeClock(now)).verify_download_grant(token, _RESOURCE)


def test_download_grant_is_not_an_access_token() -> None:
    # The separation guard (issue #2313): a grant leaked into an access log must
    # not be usable as a session credential.
    clock = _FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc))
    svc = _service(clock)
    issued = svc.issue_download_grant(_USER, _RESOURCE)
    with pytest.raises(InvalidAccessTokenError):
        svc.verify_access_token(issued.token)


def test_access_token_is_not_a_download_grant() -> None:
    # The other direction: a 15-minute session token must not open a download URL.
    clock = _FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc))
    svc = _service(clock)
    token = svc.issue_access_token(_USER)
    with pytest.raises(InvalidDownloadGrantError):
        svc.verify_download_grant(token, _RESOURCE)
