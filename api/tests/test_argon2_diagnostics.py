"""The argon2 mismatch report a refused correct password fails with (#3748).

A correct password has intermittently failed to verify against a hash the same
process had just produced. The report is what that failure carries, so each of
its sections has to be there the one time it is needed.
"""

from __future__ import annotations

import concurrent.futures
import re
import threading

import argon2
import pytest

from mc_server_dashboard_api.identity.adapters.password_hasher import _KDF_EXECUTOR
from tests import argon2_diagnostics
from tests.argon2_diagnostics import argon2_mismatch_report

_PASSWORD = "Wm7!qz#Lp2vT"
_WRONG_PASSWORD = "Np4@xZ#Lq9wR"

_STORED_ON_THREAD = "stored hash verified 5x on this thread"
_STORED_ON_EXECUTOR = "stored hash verified 3x on the KDF executor"
_FRESH_ON_THREAD = "fresh hash+verify round trips 3x on this thread"
_FRESH_ON_EXECUTOR = "fresh hash+verify round trips 3x on the KDF executor"


def _sections(report: str) -> dict[str, list[str]]:
    """The report's lines under each ``--- heading ---``, by heading."""
    sections: dict[str, list[str]] = {}
    lines: list[str] = []
    for line in report.splitlines():
        if line.startswith("--- ") and line.endswith(" ---"):
            lines = sections.setdefault(line.removeprefix("--- ")[:-4], [])
        else:
            lines.append(line)
    return sections


def test_a_refused_password_reports_every_section(
    request: pytest.FixtureRequest,
) -> None:
    stored = argon2.PasswordHasher().hash(_PASSWORD)

    report = argon2_mismatch_report(
        password=_WRONG_PASSWORD, stored_hash=stored, produced_hash=stored
    )

    # The report's own round trips are exposed to #3748, so it is the message.
    sections = _sections(report)
    assert list(sections) == [
        "stored hash",
        _STORED_ON_THREAD,
        _STORED_ON_EXECUTOR,
        _FRESH_ON_THREAD,
        _FRESH_ON_EXECUTOR,
        "process",
        "tests this process started, oldest first",
        "argon2 build",
    ], report
    assert re.search(r"taken at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00", report)
    assert f"value: {stored!r}" in sections["stored hash"]
    assert "equals the hash the test produced: True" in sections["stored hash"]
    # Each probe of the stored hash saw the wrong password refused, and the
    # executor's ran on the hasher's own KDF threads.
    refusals = sections[_STORED_ON_THREAD] + sections[_STORED_ON_EXECUTOR]
    assert len(refusals) == 8, report
    assert all(line.startswith("VerifyMismatchError(") for line in refusals), report
    on_executor = sections[_STORED_ON_EXECUTOR] + sections[_FRESH_ON_EXECUTOR]
    assert all(re.search(r"\[password-kdf_\d+\]$", line) for line in on_executor)
    # The conftest hook recorded this test as the last one this process started.
    assert sections["tests this process started, oldest first"][-1] == (
        request.node.nodeid
    )


def test_hashing_gone_bad_shows_in_the_fresh_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A process whose new hashes no longer match their password, the third
    # hypothesis: only the fresh round trips can show it.
    stored = argon2.PasswordHasher().hash(_PASSWORD)
    real_hash = argon2.PasswordHasher.hash

    def bad_hash(
        self: argon2.PasswordHasher, password: str | bytes, *, salt: bytes | None = None
    ) -> str:
        return real_hash(self, _WRONG_PASSWORD, salt=salt)

    monkeypatch.setattr(argon2.PasswordHasher, "hash", bad_hash)

    report = argon2_mismatch_report(password=_PASSWORD, stored_hash=stored)

    sections = _sections(report)
    round_trips = sections[_FRESH_ON_THREAD] + sections[_FRESH_ON_EXECUTOR]
    assert len(round_trips) == 6, report
    assert all(line.startswith("VerifyMismatchError(") for line in round_trips)
    stored_verifies = sections[_STORED_ON_THREAD] + sections[_STORED_ON_EXECUTOR]
    assert all(line.startswith("True [") for line in stored_verifies), report


def test_a_wedged_kdf_executor_still_yields_a_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pool whose threads never come free must cost one timeout, not one per
    # probe: the suite's 120 s test timeout would otherwise kill the report.
    monkeypatch.setattr(argon2_diagnostics, "_EXECUTOR_TIMEOUT_S", 0.2)
    stored = argon2.PasswordHasher().hash(_PASSWORD)
    release = threading.Event()
    parked = threading.Semaphore(0)

    def park() -> None:
        parked.release()
        release.wait()

    # The pool's four threads (max_workers in password_hasher.py), all parked.
    parkers = [_KDF_EXECUTOR.submit(park) for _ in range(4)]
    try:
        for _ in parkers:
            assert parked.acquire(timeout=10)
        report = argon2_mismatch_report(password=_PASSWORD, stored_hash=stored)
    finally:
        release.set()
        concurrent.futures.wait(parkers)

    sections = _sections(report)
    on_executor = sections[_STORED_ON_EXECUTOR] + sections[_FRESH_ON_EXECUTOR]
    assert on_executor[0].startswith("TimeoutError: no result within 0.2 s"), report
    assert on_executor[1:] == ["skipped: the KDF executor timed out above"] * 5
    on_thread = sections[_STORED_ON_THREAD] + sections[_FRESH_ON_THREAD]
    assert all(line.startswith("True [") for line in on_thread), report


def test_unreadable_build_info_does_not_break_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A report that raised would replace the failure it was meant to explain.
    def unreadable() -> str:
        raise RuntimeError("metadata gone")

    monkeypatch.setattr(argon2_diagnostics, "_bindings_wheel_tags", unreadable)
    stored = argon2.PasswordHasher().hash(_PASSWORD)

    report = argon2_mismatch_report(password=_PASSWORD, stored_hash=stored)

    assert _sections(report)["argon2 build"] == [
        "unavailable: RuntimeError('metadata gone')"
    ]
