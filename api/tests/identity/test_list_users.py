"""Unit tests for the ListUsers use case (admin paginated listing, #278)."""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.identity.application.list_users import ListUsers
from tests.identity.fakes import FakeUnitOfWork, make_user

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)


def _use_case(uow: FakeUnitOfWork) -> ListUsers:
    return ListUsers(uow=uow)


async def test_returns_page_and_total_ordered_by_created_at() -> None:
    first = make_user(username="first", email="first@example.com", now=_NOW)
    second = make_user(
        username="second",
        email="second@example.com",
        now=_NOW + dt.timedelta(minutes=1),
    )
    third = make_user(
        username="third", email="third@example.com", now=_NOW + dt.timedelta(minutes=2)
    )
    uow = FakeUnitOfWork()
    for user in (third, first, second):
        uow.users.seed(user)

    page = await _use_case(uow)(limit=2, offset=0)

    assert page.total == 3
    assert [u.username.value for u in page.users] == ["first", "second"]


async def test_offset_returns_remainder() -> None:
    users = [
        make_user(
            username=f"u{i}",
            email=f"u{i}@example.com",
            now=_NOW + dt.timedelta(minutes=i),
        )
        for i in range(3)
    ]
    uow = FakeUnitOfWork()
    for user in users:
        uow.users.seed(user)

    page = await _use_case(uow)(limit=2, offset=2)

    assert page.total == 3
    assert [u.username.value for u in page.users] == ["u2"]
