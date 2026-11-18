"""The login's dummy hash through the real wiring (issue #3832).

The unknown-user login path verifies against a dummy hash, built once per process
from a throwaway plaintext. These tests drive ``POST /auth/login`` through the
real app, the real ``get_login`` dependency and a real database, with argon2's
``hash`` patched to count (and optionally fail) the dummy builds:

- a failure to build it answers the same ``503 password_verification_unavailable``
  as any other hasher failure, and is not remembered: the next login builds it
  and succeeds;
- concurrent first logins build it once.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service).
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import argon2
import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api import dependencies
from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.dependencies import get_audit_recorder
from mc_server_dashboard_api.identity.adapters.password_hasher import (
    Argon2PasswordHasher,
)
from mc_server_dashboard_api.identity.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.identity.application.login import DummyPasswordHash
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_PASSWORD = "Wm7!qz#Lp2vT"


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


@pytest.fixture
async def alice(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> User:
    """Seed ``alice`` and point the app at the real database."""

    assert _DB_URL is not None
    monkeypatch.setenv("MCD_API_DATABASE__URL", _DB_URL)
    now = dt.datetime.now(tz=dt.timezone.utc)
    user = User(
        id=UserId.new(),
        username=Username("alice"),
        email=EmailAddress("alice@example.com"),
        password_hash=argon2.PasswordHasher().hash(_PASSWORD),
        created_at=now,
        updated_at=now,
    )
    async with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
        await uow.users.add(user)
        await uow.commit()
    return user


@pytest.fixture
def first_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the process a dummy hash not built yet, so the next login is the first."""

    monkeypatch.setitem(
        dependencies._DUMMY_PASSWORD_HASHES,
        "argon2",
        DummyPasswordHash(Argon2PasswordHasher(), dependencies._DUMMY_VERIFY_PLAINTEXT),
    )


def _count_dummy_builds(
    monkeypatch: pytest.MonkeyPatch, *, fail_first: bool = False, hold_s: float = 0.0
) -> list[str]:
    """Patch argon2 to record the thread of each dummy build.

    The first build fails if ``fail_first``, as libargon2 does when it cannot
    allocate its memory. ``hold_s`` keeps each build running that long, so
    concurrent logins overlap.
    """

    builds: list[str] = []
    real_hash = argon2.PasswordHasher.hash

    def _hash(
        self: argon2.PasswordHasher, password: str | bytes, *, salt: bytes | None = None
    ) -> str:
        if password != dependencies._DUMMY_VERIFY_PLAINTEXT:
            return real_hash(self, password, salt=salt)
        builds.append(threading.current_thread().name)
        time.sleep(hold_s)
        if fail_first and len(builds) == 1:
            raise argon2.exceptions.HashingError("Memory allocation error")
        return real_hash(self, password, salt=salt)

    monkeypatch.setattr(argon2.PasswordHasher, "hash", _hash)
    return builds


@contextmanager
def _client() -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_audit_recorder] = RecordingAuditRecorder
    with TestClient(app) as client:
        yield client


def _log_in(client: TestClient) -> httpx2.Response:
    return client.post(
        "/api/auth/login", json={"username": "alice", "password": _PASSWORD}
    )


@pytest.mark.usefixtures("alice", "first_login")
def test_a_dummy_hash_build_failure_answers_503_until_a_build_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds = _count_dummy_builds(monkeypatch, fail_first=True)

    with _client() as client:
        failed = _log_in(client)
        recovered = _log_in(client)
        again = _log_in(client)

    assert failed.status_code == 503
    assert failed.json()["reason"] == "password_verification_unavailable"
    assert recovered.status_code == 200
    assert again.status_code == 200
    # The failure was not remembered, and the success was: two builds, not three.
    assert len(builds) == 2
    # Off the event loop, on the dedicated KDF pool (#1696).
    assert all(name.startswith("password-kdf") for name in builds), builds


@pytest.mark.usefixtures("alice", "first_login")
def test_concurrent_first_logins_build_the_dummy_hash_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds = _count_dummy_builds(monkeypatch, hold_s=0.3)

    with _client() as client, ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: _log_in(client), range(4)))

    assert [r.status_code for r in responses] == [200] * 4
    assert len(builds) == 1
