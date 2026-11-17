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

import datetime as dt
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

_EXECUTOR_TIMEOUT_S = 5.0
"""How long a probe waits for the KDF executor. The first timeout skips the
probes after it, so even a wedged pool keeps the report far inside the suite's
120 s test timeout, whose ``Failed`` no ``except Exception`` here would catch."""


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


class _KdfExecutorProbes:
    """:func:`_probe` on the hasher's own KDF thread pool, as production runs it."""

    def __init__(self) -> None:
        self._timed_out = False

    def __call__(self, call: Callable[[], bool]) -> str:
        if self._timed_out:
            return "skipped: the KDF executor timed out above"
        try:
            future = _KDF_EXECUTOR.submit(_probe, call)
            return future.result(timeout=_EXECUTOR_TIMEOUT_S)
        except TimeoutError:
            future.cancel()  # a probe still queued must not run once the pool frees
            self._timed_out = True
            return f"TimeoutError: no result within {_EXECUTOR_TIMEOUT_S} s"
        except Exception as exc:  # a shut-down pool refuses the submit: a finding
            return f"{exc!r} [KDF executor]"


def _bindings_wheel_tags() -> str:
    wheel = importlib.metadata.distribution("argon2-cffi-bindings").read_text("WHEEL")
    tags = [
        line.removeprefix("Tag: ")
        for line in (wheel or "").splitlines()
        if line.startswith("Tag: ")
    ]
    return ", ".join(tags) or "unknown"


def _build_info() -> list[str]:
    version = importlib.metadata.version
    try:
        return [
            f"argon2-cffi {version('argon2-cffi')}",
            f"argon2-cffi-bindings {version('argon2-cffi-bindings')}"
            f" ({_bindings_wheel_tags()})",
            f"Argon2 algorithm version: {argon2.low_level.ARGON2_VERSION}",
        ]
    except Exception as exc:  # a report that raised would replace the failure
        return [f"unavailable: {exc!r}"]


def argon2_mismatch_report(
    *,
    password: str,
    stored_hash: str,
    produced_hash: str | None = None,
    skip_probes: str | None = None,
) -> str:
    """Probe argon2 after it refused ``password`` against ``stored_hash``.

    ``produced_hash`` is the hash the test made, when it is not the one read
    back. The probes run in the failing test, so it fails as before, only
    slower and with this report. ``skip_probes`` says why a refusal argon2
    never saw needs none of them.
    """
    taken_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
    verify = functools.partial(_verify, password, stored_hash)
    round_trip = functools.partial(_round_trip, password)
    on_executor = _KdfExecutorProbes()

    def probes(
        run: Callable[[Callable[[], bool]], str], call: Callable[[], bool], times: int
    ) -> list[str]:
        if skip_probes is not None:
            return [f"skipped: {skip_probes}"]
        return [run(call) for _ in range(times)]

    same = "not available" if produced_hash is None else produced_hash == stored_hash
    return "\n".join(
        [
            f"argon2 mismatch diagnostics (issue #3748), taken at {taken_at};",
            "how to read them: docs/dev/AGENTS.md Section 3.",
            "--- stored hash ---",
            f"value: {stored_hash!r}",
            f"length: {len(stored_hash)}",
            f"sha256: {hashlib.sha256(stored_hash.encode()).hexdigest()}",
            f"equals the hash the test produced: {same}",
            "--- stored hash verified 5x on this thread ---",
            *probes(_probe, verify, 5),
            "--- stored hash verified 3x on the KDF executor ---",
            *probes(on_executor, verify, 3),
            "--- fresh hash+verify round trips 3x on this thread ---",
            *probes(_probe, round_trip, 3),
            "--- fresh hash+verify round trips 3x on the KDF executor ---",
            *probes(on_executor, round_trip, 3),
            "--- process ---",
            f"pid: {os.getpid()}",
            f"xdist worker: {os.environ.get('PYTEST_XDIST_WORKER', 'none')}",
            f"threads: {', '.join(t.name for t in threading.enumerate())}",
            "--- tests this process started, oldest first ---",
            *RECENT_NODE_IDS,
            "--- argon2 build ---",
            *_build_info(),
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
        if exc.retry_after is None:
            headline = "Login refused the correct password of a seeded user."
            skip_probes = None
        else:
            # Only a lockout or an IP throttle sets it (login.py), never argon2.
            headline = (
                f"Login refused by a lockout or throttle (retry_after="
                f"{exc.retry_after}), not by argon2: this is not the #3748 flake."
            )
            skip_probes = "a lockout or throttle refused this login, not argon2"
        report = argon2_mismatch_report(
            password=password,
            stored_hash=stored,
            produced_hash=None if note else user.password_hash,
            skip_probes=skip_probes,
        )
        raise AssertionError(f"{headline}\n{note}{report}") from exc
