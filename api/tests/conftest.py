"""Shared test setup.

A dummy database URL satisfies the fail-fast config loader so the app factory
builds; SQLAlchemy engines are lazy, so no connection is opened. Tests that
exercise the DB seam override the :class:`DatabasePing` Port with a fake, so the
dummy URL is never dialed (NFR-TEST-1).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from fastapi import FastAPI

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.dependencies import get_audit_recorder

_SCRATCH_DB_URL: str | None = None
"""The per-run scratch database created for this session, if any (issue #379)."""


def _is_xdist_controller(config: pytest.Config) -> bool:
    """True only on the xdist controller process (``-n`` active, not a worker).

    Under ``pytest -n``, ``pytest_configure`` runs once on the controller and
    once per worker, but only workers run tests — so a scratch DB created on the
    controller is never used, just created and dropped again (issue #1742).

    This mirrors xdist's own ``is_xdist_controller`` helper, reimplemented
    against ``config`` because the configure hook receives a ``Config`` object
    whereas that helper only accepts a ``request``/``session``. xdist workers
    carry ``config.workerinput``; the controller does not but has ``-n`` active
    (``option.dist != "no"``). A serial run has neither, so it is *not* a
    controller and still creates its DB. ``dist`` is read via ``getattr`` so an
    environment where xdist never registered its options defaults to serial —
    we skip only when positively identified as the controller.
    """
    is_worker = hasattr(config, "workerinput")
    return not is_worker and getattr(config.option, "dist", "no") != "no"


def pytest_configure(config: pytest.Config) -> None:
    """Redirect ``MCD_TEST_DATABASE_URL`` to a fresh per-run database (#379).

    The DB-gated integration tests read ``MCD_TEST_DATABASE_URL`` at import time
    and run ``downgrade base`` / ``upgrade head`` against it. Treating that URL
    as a *base/maintenance* connection and creating a unique
    ``<dbname>_<short-uuid>`` database per session makes parallel runs (e.g.
    agent worktrees sharing one Postgres) disjoint, so they can no longer race
    on the schema or leave orphan tables behind. Runs before collection, so the
    test modules import the per-run URL.
    """
    global _SCRATCH_DB_URL
    base_url = os.environ.get("MCD_TEST_DATABASE_URL_BASE") or os.environ.get(
        "MCD_TEST_DATABASE_URL"
    )
    if base_url is None:
        return
    if _is_xdist_controller(config):
        # The controller runs no tests; each worker creates its own scratch DB
        # in its own pytest_configure. Skipping here avoids a wasted create/drop
        # round-trip per run (issue #1742).
        return
    from tests.integration.scratch_db import (
        create_scratch_database,
        derive_scratch_url,
    )

    scratch_url = derive_scratch_url(base_url, uuid.uuid4().hex[:12])
    create_scratch_database(base_url, scratch_url)
    os.environ["MCD_TEST_DATABASE_URL_BASE"] = base_url
    os.environ["MCD_TEST_DATABASE_URL"] = scratch_url
    _SCRATCH_DB_URL = scratch_url


def pytest_unconfigure(config: pytest.Config) -> None:
    """Drop the per-run scratch database created in ``pytest_configure`` (#379)."""
    if _SCRATCH_DB_URL is None:
        return
    base_url = os.environ["MCD_TEST_DATABASE_URL_BASE"]
    from tests.integration.scratch_db import drop_scratch_database

    drop_scratch_database(base_url, _SCRATCH_DB_URL)


def _set_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the default environment the app factory needs to build.

    Shared by the per-test :func:`_dummy_database_url` fixture and the
    session-scoped :func:`_session_app` fixture, so the single shared app builds
    against exactly the same defaults an individually built app would (#1736).
    """
    monkeypatch.setenv(
        "MCD_API_DATABASE__URL", "postgresql+asyncpg://test:test@localhost/test"
    )
    # The auth endpoints require a token signing key to mount (CONFIGURATION.md
    # Section 5.3); a dummy value satisfies the fail-fast check. Tests that need
    # to verify the missing-key behaviour clear it explicitly.
    monkeypatch.setenv(
        "MCD_API_AUTH__TOKEN__SIGNING_KEY", "test-signing-key-of-at-least-32by"
    )
    # Disable the control-plane gRPC server by default so building the app under
    # TestClient does not bind a port (CONFIGURATION.md Section 5.1). The fleet
    # gRPC integration tests construct the server directly; tests that need the
    # control-plane fail-fast behaviour set these explicitly.
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "false")
    # Opt the control channel into a plaintext listener by default so tests that
    # DO enable the control plane build without TLS material (CONFIGURATION.md
    # Section 5.1, required-unless-insecure). Tests that exercise the TLS posture
    # or the fail-fast set these explicitly.
    monkeypatch.setenv("MCD_API_CONTROL__TLS__INSECURE", "true")


@pytest.fixture(autouse=True)
def _dummy_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_test_env(monkeypatch)


@pytest.fixture(scope="session")
def _session_app() -> FastAPI:
    """Build the FastAPI app once per xdist worker (issue #1736).

    ``create_app`` costs ~0.4 s warm — almost all of it FastAPI's per-route
    dependency introspection during ``include_router`` — so building it once per
    test dominated the api suite's CPU budget. Endpoint tests vary the app only
    through ``app.dependency_overrides`` (never through ``create_app`` arguments),
    so a single app shared across a worker's tests is sound; the :func:`shared_app`
    wrapper clears those overrides around each test.

    Consume this only via :func:`shared_app`. A test that needs a custom
    ``settings``, or that mutates the app itself (middleware, ``app.state``, route
    registration), must still build its own app with ``create_app``.

    The environment is applied for the duration of the build only: ``create_app``
    reads settings eagerly and captures them, so the process environment need not
    stay patched afterwards (the per-test :func:`_dummy_database_url` keeps it
    consistent for request handling).
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        _set_test_env(monkeypatch)
        return create_app()


class _NoOpAuditRecorder:
    """Silently discards audit events (issue #1758).

    Injected as the default :class:`AuditRecorder` in endpoint tests so the
    real :class:`LoggingAuditRecorder` never dials the dummy test DB.  Tests
    that assert on audit events override this with
    :class:`~tests.audit.fakes.RecordingAuditRecorder` themselves.
    """

    async def record(self, event: object) -> None:  # noqa: ARG002
        pass


class _BaselineOverrides(dict[Any, Any]):
    """A dict that re-applies baseline entries after ``.clear()`` (#1758).

    Endpoint-test ``_app()`` helpers call ``dependency_overrides.clear()`` to
    prevent override leakage between tests.  The no-op audit recorder must
    survive those clears so the real recorder never dials the dummy test DB.
    """

    def __init__(self, baseline: Mapping[Any, Any]) -> None:
        super().__init__(baseline)
        self._baseline = dict(baseline)

    def clear(self) -> None:
        super().clear()
        self.update(self._baseline)


_noop_audit_recorder = _NoOpAuditRecorder()


@pytest.fixture
def shared_app(_session_app: FastAPI) -> Iterator[FastAPI]:
    """The per-worker session app with ``dependency_overrides`` cleared each test.

    Function-scoped wrapper over :func:`_session_app`: it empties the override map
    on entry and again on exit, so one test's fakes can never leak into another.
    Endpoint-test modules bind this (typically via a module-level ``autouse``
    fixture) and register their fakes on the yielded app.

    The baseline includes a no-op :class:`AuditRecorder` so the real recorder
    never dials the dummy test DB (issue #1758).  Tests that need to inspect
    audit events override ``get_audit_recorder`` explicitly.
    """
    _session_app.dependency_overrides = _BaselineOverrides(
        {get_audit_recorder: lambda: _noop_audit_recorder}
    )
    yield _session_app
    _session_app.dependency_overrides = {}
