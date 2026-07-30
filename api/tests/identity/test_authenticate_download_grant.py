"""Unit tests for the AuthenticateDownloadGrant use case (issues #2313, #2373)."""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.identity.application.authenticate_download_grant import (
    AuthenticateDownloadGrant,
)
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidDownloadCookieError,
    InvalidDownloadGrantError,
)
from tests.identity.fakes import FakeTokenService, FakeUnitOfWork, make_user

_RESOURCE = "backup-download:c:s:b"


def _auth(uow: FakeUnitOfWork) -> AuthenticateDownloadGrant:
    return AuthenticateDownloadGrant(uow=uow, tokens=FakeTokenService())


async def test_valid_grant_returns_user() -> None:
    user = make_user()
    uow = FakeUnitOfWork()
    uow.users.seed(user)

    resolved = await _auth(uow)(
        grant=f"grant::{_RESOURCE}::{user.id.value}", resource=_RESOURCE
    )

    assert resolved.id == user.id


async def test_invalid_grant_is_rejected() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(InvalidDownloadGrantError):
        await _auth(uow)(grant="garbage", resource=_RESOURCE)


async def test_grant_for_another_resource_is_rejected() -> None:
    user = make_user()
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    with pytest.raises(InvalidDownloadGrantError):
        await _auth(uow)(
            grant=f"grant::{_RESOURCE}::{user.id.value}",
            resource="backup-download:c:s:other",
        )


async def test_grant_for_missing_user_is_rejected() -> None:
    user = make_user()  # not seeded
    uow = FakeUnitOfWork()
    with pytest.raises(InvalidDownloadGrantError):
        await _auth(uow)(
            grant=f"grant::{_RESOURCE}::{user.id.value}", resource=_RESOURCE
        )


async def test_grant_for_deactivated_user_is_rejected() -> None:
    # A grant outlives its issuance only as long as its subject stays usable: a
    # deactivation between mint and redemption invalidates it, same as #278 does
    # for an outstanding access token.
    user = make_user(active=False)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    with pytest.raises(InvalidDownloadGrantError):
        await _auth(uow)(
            grant=f"grant::{_RESOURCE}::{user.id.value}", resource=_RESOURCE
        )


# --- the cookie transport (issue #2373) ------------------------------------


async def test_valid_cookie_returns_user() -> None:
    user = make_user()
    uow = FakeUnitOfWork()
    uow.users.seed(user)

    resolved = await _auth(uow).from_cookie(
        cookie=f"cookie::{_RESOURCE}::{user.id.value}", resource=_RESOURCE
    )

    assert resolved.id == user.id


async def test_cookie_for_another_resource_is_rejected() -> None:
    user = make_user()
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    with pytest.raises(InvalidDownloadCookieError):
        await _auth(uow).from_cookie(
            cookie=f"cookie::{_RESOURCE}::{user.id.value}",
            resource="backup-download:c:s:other",
        )


async def test_a_grant_is_not_accepted_from_the_cookie_transport() -> None:
    user = make_user()
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    with pytest.raises(InvalidDownloadCookieError):
        await _auth(uow).from_cookie(
            cookie=f"grant::{_RESOURCE}::{user.id.value}", resource=_RESOURCE
        )


async def test_cookie_for_deactivated_user_is_rejected() -> None:
    # The cookie lives longer than the grant, so the subject re-check matters more:
    # a deactivation must end it well before its own TTL.
    user = make_user(active=False)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    with pytest.raises(InvalidDownloadCookieError):
        await _auth(uow).from_cookie(
            cookie=f"cookie::{_RESOURCE}::{user.id.value}", resource=_RESOURCE
        )


async def test_cookie_for_missing_user_is_rejected() -> None:
    user = make_user()  # not seeded
    uow = FakeUnitOfWork()
    with pytest.raises(InvalidDownloadCookieError):
        await _auth(uow).from_cookie(
            cookie=f"cookie::{_RESOURCE}::{user.id.value}", resource=_RESOURCE
        )
