"""Capture-on-failure diagnostics for a correct password argon2 refused (#3748).

A correct password has intermittently failed to verify against a hash the same
process had just produced, about once per 200 exposed tests, without
reproducing in isolation. When it happens, the tests that log in or verify with
the real :class:`Argon2PasswordHasher` fail with :func:`argon2_mismatch_report`
as the message, which tells apart a verify-time transient, a bad stored hash
and a persistently bad process state. docs/dev/AGENTS.md Section 3 says how to
read it.

Nothing here runs on a passing test except the append to
:data:`RECENT_NODE_IDS`.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.metadata
import os
import threading
from collections import deque
from collections.abc import Callable

import argon2
from sqlalchemy.ext.asyncio import AsyncEngine

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.password_hasher import _KDF_EXECUTOR
from mc_server_dashboard_api.identity.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.identity.application.login import Login, LoginResult
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import InvalidCredentialsError

RECENT_NODE_IDS: deque[str] = deque(maxlen=40)
"""The last tests this process started, oldest first; ``tests/conftest.py``
appends each one from ``pytest_runtest_logstart``."""

_EXECUTOR_TIMEOUT_S = 30.0


def _verify(password: str, stored_hash: str) -> bool:
    return argon2.PasswordHasher().verify(stored_hash, password)


def _round_trip(password: str) -> bool:
    hasher = argon2.PasswordHasher()
    return hasher.verify(hasher.hash(password), password)


def _probe(call: Callable[[], bool]) -> str:
    """Run ``call`` and name its outcome, a mismatch included, and its thread."""
    try:
        outcome = repr(call())
    except Exception as exc:  # the outcome is the finding, never a new failure
        outcome = repr(exc)
    return f"{outcome} [{threading.current_thread().name}]"


def _probe_on_kdf_executor(call: Callable[[], bool]) -> str:
    """:func:`_probe` on the hasher's own KDF thread pool, as production runs it."""
    try:
        return _KDF_EXECUTOR.submit(_probe, call).result(timeout=_EXECUTOR_TIMEOUT_S)
    except Exception as exc:  # a wedged or shut-down pool is itself a finding
        return f"{exc!r} [KDF executor]"


def _bindings_wheel_tags() -> str:
    wheel = importlib.metadata.distribution("argon2-cffi-bindings").read_text("WHEEL")
    tags = [
        line.removeprefix("Tag: ")
        for line in (wheel or "").splitlines()
        if line.startswith("Tag: ")
    ]
    return ", ".join(tags) or "unknown"


def argon2_mismatch_report(
    *, password: str, stored_hash: str, produced_hash: str | None = None
) -> str:
    """Probe argon2 after it refused ``password`` against ``stored_hash``.

    ``produced_hash`` is the hash the test made, when it is not the one read
    back. The probes run in the failing test, so it fails as before, only
    slower and with this report.
    """
    verify = functools.partial(_verify, password, stored_hash)
    round_trip = functools.partial(_round_trip, password)
    same = "not available" if produced_hash is None else produced_hash == stored_hash
    version = importlib.metadata.version
    return "\n".join(
        [
            "argon2 refused a correct password (issue #3748); how to read this:",
            "docs/dev/AGENTS.md Section 3.",
            "--- stored hash ---",
            f"value: {stored_hash!r}",
            f"length: {len(stored_hash)}",
            f"sha256: {hashlib.sha256(stored_hash.encode()).hexdigest()}",
            f"equals the hash the test produced: {same}",
            "--- stored hash verified 5x on this thread ---",
            *(_probe(verify) for _ in range(5)),
            "--- stored hash verified 3x on the KDF executor ---",
            *(_probe_on_kdf_executor(verify) for _ in range(3)),
            "--- fresh hash+verify round trips 3x on this thread ---",
            *(_probe(round_trip) for _ in range(3)),
            "--- fresh hash+verify round trips 3x on the KDF executor ---",
            *(_probe_on_kdf_executor(round_trip) for _ in range(3)),
            "--- process ---",
            f"pid: {os.getpid()}",
            f"xdist worker: {os.environ.get('PYTEST_XDIST_WORKER', 'none')}",
            f"threads: {', '.join(t.name for t in threading.enumerate())}",
            "--- tests this process started, oldest first ---",
            *RECENT_NODE_IDS,
            "--- argon2 build ---",
            f"argon2-cffi {version('argon2-cffi')}",
            f"argon2-cffi-bindings {version('argon2-cffi-bindings')}"
            f" ({_bindings_wheel_tags()})",
            f"Argon2 algorithm version: {argon2.low_level.ARGON2_VERSION}",
        ]
    )


async def _read_stored_hash(engine: AsyncEngine, user: User) -> str:
    async with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
        stored = await uow.users.get_by_username(user.username)
    if stored is None:
        raise LookupError(f"no user {user.username.value!r} in the database")
    return stored.password_hash


async def log_in_seeded(
    login: Login,
    engine: AsyncEngine,
    user: User,
    password: str,
    *,
    ip: str | None = None,
) -> LoginResult:
    """Log ``user`` in with its correct ``password``; on a refusal, fail with
    :func:`argon2_mismatch_report` over the hash read back from the database."""
    __tracebackhide__ = True  # the failure points at the test's own login line
    try:
        return await login(username=user.username.value, password=password, ip=ip)
    except InvalidCredentialsError as exc:
        note = ""
        try:
            stored = await _read_stored_hash(engine, user)
        except Exception as read_exc:  # never let the diagnostics mask the refusal
            note = (
                f"Reading the stored hash back failed ({read_exc!r}), so this"
                " report probes the hash the test produced instead.\n"
            )
            stored = user.password_hash
        report = argon2_mismatch_report(
            password=password,
            stored_hash=stored,
            produced_hash=None if note else user.password_hash,
        )
        raise AssertionError(
            f"Login refused the correct password of a seeded user.\n{note}{report}"
        ) from exc
