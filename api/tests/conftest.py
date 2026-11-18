"""Shared test setup.

A dummy database URL satisfies the fail-fast config loader so the app factory
builds; SQLAlchemy engines are lazy, so no connection is opened. Tests that
exercise the DB seam override the :class:`DatabasePing` Port with a fake, so the
dummy URL is never dialed (NFR-TEST-1).
"""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from mc_server_dashboard_api import dependencies
from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.dependencies import get_audit_recorder
from mc_server_dashboard_api.identity.application.authenticate_request import (
    AuthenticateRequest,
    Authentication,
)
from tests.argon2_diagnostics import RECENT_NODE_IDS
from tests.client_utils import bind_exit_stack
from tests.identity.fakes import FakeTokenService, FakeUnitOfWork, make_user
from tests.store_ownership import FakeStoreOwnership

# ``--run-order-log``, which the gate's api run passes (issue #3750).
pytest_plugins = ["tests.run_order_log"]

_SCRATCH_DB_URL: str | None = None
"""The per-run scratch database created for this session, if any (issue #379)."""

_TMP_ROOT_ENV = "MCSD_TEST_TMPDIR"
"""Names the suite's tmp root explicitly; it wins over the tmpfs (issue #3722)."""

_TMPFS = Path("/dev/shm")
_TMPFS_MIN_FREE = 1 << 30
"""Room ``/dev/shm`` needs before the suite uses it. A container's 64 MiB
default would run out mid-suite; the default tmp dir is used instead."""


def _tmpfs_or_default(tmpfs: Path, min_free: int) -> str:
    """``tmpfs`` when it is a writable directory with ``min_free`` bytes free,
    otherwise the default tmp dir."""
    try:
        stats = os.statvfs(tmpfs)
    except OSError:
        return tempfile.gettempdir()
    if (
        os.access(tmpfs, os.W_OK | os.X_OK)
        and stats.f_bavail * stats.f_frsize >= min_free
    ):
        return str(tmpfs)
    return tempfile.gettempdir()


def _select_tmp_root() -> None:
    """Point ``TMPDIR`` and with it pytest's basetemp at a tmpfs (issue #3722).

    The fs-backed storage tests fsync for real, and on disk under host I/O load
    a single fsync has blocked for over 120 s. On a tmpfs an fsync does not
    wait on the disk. Tests that check fsync ordering patch ``os.fsync``, so
    they hold on any filesystem.

    pytest roots its numbered ``pytest-of-<user>/pytest-N`` basetemp, and
    its retention of the last three, at ``tempfile.gettempdir()``; xdist
    workers get a directory under the controller's. The choice is exported, so
    the workers, which inherit the controller's environment, use the same root.
    """
    root = os.environ.get(_TMP_ROOT_ENV) or _tmpfs_or_default(_TMPFS, _TMPFS_MIN_FREE)
    if not os.path.isdir(root):
        raise pytest.UsageError(f"{_TMP_ROOT_ENV}={root} is not a directory")
    os.environ[_TMP_ROOT_ENV] = root
    os.environ["TMPDIR"] = root
    tempfile.tempdir = root


def pytest_report_header(config: pytest.Config) -> str:
    return f"tmp root: {os.environ[_TMP_ROOT_ENV]} (override: {_TMP_ROOT_ENV})"


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

    It first selects the suite's tmp root (:func:`_select_tmp_root`).
    """
    global _SCRATCH_DB_URL
    _select_tmp_root()
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


def pytest_runtest_logstart(nodeid: str) -> None:
    """Remember each test this process starts, for the argon2 report (#3748)."""
    RECENT_NODE_IDS.append(nodeid)


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
    # Mount the OpenAPI schema and docs pages by default (off in code, issue
    # #2568) so the shared app carries every route the factory can register and
    # the route-walking tests see the docs mount. The gating tests set this
    # explicitly.
    monkeypatch.setenv("MCD_API_DOCS__ENABLED", "true")
    # Stand in for the store owner marker (issue #3870). The real claim reads the
    # database's installation id and writes a marker at the store, and the suite
    # gives the app neither: the dummy database is never dialed, and the default
    # fs root is ``./data`` in the working tree. The stand-in lets every sweep
    # and GC run, as before; tests/test_store_ownership_wiring.py puts the real
    # marker back.
    monkeypatch.setattr(
        "mc_server_dashboard_api.app.FsStoreOwnership", _standin_ownership
    )
    monkeypatch.setattr(
        "mc_server_dashboard_api.app.ObjectStoreOwnership", _standin_ownership
    )
    monkeypatch.setattr(
        "mc_server_dashboard_api.app.read_installation_id", _no_installation_id
    )


def _standin_ownership(*_args: object, **_kwargs: object) -> FakeStoreOwnership:
    return FakeStoreOwnership()


async def _no_installation_id(_session_factory: object) -> uuid.UUID:
    return uuid.UUID(int=0)


@pytest.fixture(autouse=True)
def _dummy_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_test_env(monkeypatch)


@pytest.fixture(autouse=True)
def _client_exit_stack() -> Iterator[None]:
    """Own the per-test ExitStack that ``enter_client`` enters clients into (#1980).

    Endpoint ``_client`` helpers acquire their ``TestClient`` via
    :func:`tests.client_utils.enter_client`, which registers the client on this
    stack (running lifespan startup). Closing the stack at teardown runs each
    client's lifespan shutdown AFTER the test body, so requests run with the
    lifespan open rather than against a torn-down app.
    """
    with ExitStack() as stack:
        bind_exit_stack(stack)
        try:
            yield
        finally:
            bind_exit_stack(None)


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


@pytest.fixture
def body_credential(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """An ``Authorization`` header the body limit counts as a valid credential.

    A test that signs its caller in through ``dependency_overrides`` sends no
    credential, and the body-limit middleware, which judges one from the
    request's headers alone, holds such a request to the anonymous limit (issue
    #3561). With this header, and the middleware's token check run over the fake
    identity store (as the events socket's tests do for its own), the request
    keeps its route's limit; the route still answers through the overrides.
    """

    user = make_user()
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    tokens = FakeTokenService()
    use_case = AuthenticateRequest(uow=uow, tokens=tokens)

    async def authenticate(_request: object, token: str) -> Authentication:
        return await use_case(access_token=token)

    monkeypatch.setattr(dependencies, "_authenticate_body_token", authenticate)
    return {"Authorization": f"Bearer {tokens.issue_access_token(user.id)}"}
