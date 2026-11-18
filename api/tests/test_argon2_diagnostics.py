"""The argon2 mismatch report a refused correct password fails with (#3748).

A correct password has intermittently failed to verify against a hash the same
process had just produced. The report is what that failure carries, so each of
its sections has to be there the one time it is needed.
"""

from __future__ import annotations

import concurrent.futures
import re
import resource
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import argon2
import pytest

from mc_server_dashboard_api.identity.adapters.password_hasher import _KDF_EXECUTOR
from tests import argon2_diagnostics
from tests.argon2_diagnostics import RusageWindow, argon2_mismatch_report

_PASSWORD = "Wm7!qz#Lp2vT"
_WRONG_PASSWORD = "Np4@xZ#Lq9wR"

_STORED_ON_THREAD = "stored hash verified 5x on this thread"
_STORED_ON_EXECUTOR = "stored hash verified 3x on the KDF executor"
_FRESH_ON_THREAD = "fresh hash+verify round trips 3x on this thread"
_FRESH_ON_EXECUTOR = "fresh hash+verify round trips 3x on the KDF executor"
_HOST_KEYS = [
    "pressure cpu",
    "pressure memory",
    "pressure io",
    "vmstat",
    "rusage over the refused call",
    "native threads",
]


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
        "host",
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
    assert "rusage over the refused call: not measured" in sections["host"]
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
    probes: list[concurrent.futures.Future[str]] = []
    real_submit = _KDF_EXECUTOR.submit

    def recording_submit(
        fn: Callable[..., str], /, *args: object
    ) -> concurrent.futures.Future[str]:
        future = real_submit(fn, *args)
        probes.append(future)
        return future

    monkeypatch.setattr(_KDF_EXECUTOR, "submit", recording_submit)
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
    # The timed-out probe was withdrawn, so it does not run an argon2 hash on
    # the freed pool in the middle of whatever test comes next.
    assert [probe.cancelled() for probe in probes] == [True]


def test_a_shut_down_kdf_executor_still_yields_a_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pool that refuses new work is itself a finding, not a reason for the
    # report to raise in place of the failure it explains.
    shut_down = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    shut_down.shutdown()
    monkeypatch.setattr(argon2_diagnostics, "_KDF_EXECUTOR", shut_down)
    stored = argon2.PasswordHasher().hash(_PASSWORD)

    report = argon2_mismatch_report(password=_PASSWORD, stored_hash=stored)

    sections = _sections(report)
    on_executor = sections[_STORED_ON_EXECUTOR] + sections[_FRESH_ON_EXECUTOR]
    assert len(on_executor) == 6, report
    assert all(
        line.startswith("RuntimeError(") and line.endswith(" [KDF executor]")
        for line in on_executor
    ), report


def test_skipped_probes_say_why_in_every_probe_section() -> None:
    # A login refused by a lockout never reached argon2, so its report skips
    # the probes, which cost 14 argon2 hashes, and says so.
    stored = argon2.PasswordHasher().hash(_PASSWORD)

    report = argon2_mismatch_report(
        password=_PASSWORD, stored_hash=stored, skip_probes="a lockout refused it"
    )

    sections = _sections(report)
    for heading in (
        _STORED_ON_THREAD,
        _STORED_ON_EXECUTOR,
        _FRESH_ON_THREAD,
        _FRESH_ON_EXECUTOR,
    ):
        assert sections[heading] == ["skipped: a lockout refused it"], report


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


def test_the_host_section_reads_the_host_around_the_refused_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Whether the host was swapping, stalled or preempting the process when
    # argon2 went wrong (#3748), read before the probes add their own load. The
    # kernel's files are faked: not every kernel has pressure stall information.
    proc = tmp_path / "proc"
    (proc / "pressure").mkdir(parents=True)
    for kind, avg10 in (("cpu", "41.66"), ("memory", "0.00"), ("io", "12.50")):
        (proc / "pressure" / kind).write_text(
            f"some avg10={avg10} avg60=1.00 avg300=2.00 total=3\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        )
    (proc / "vmstat").write_text("nr_free_pages 9\npswpin 17\npswpout 23\npgfault 5\n")
    for tid, name in (("12", "grpc_global_tim"), ("3", "python"), ("7", "ended")):
        (proc / "self" / "task" / tid).mkdir(parents=True)
        (proc / "self" / "task" / tid / "comm").write_text(f"{name}\n")
    # Thread 7 ends between the listing and the read, which the kernel answers
    # with ESRCH: it drops out of the list, and the others stay.
    ended = proc / "self" / "task" / "7" / "comm"
    read_text = Path.read_text

    def ending_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == ended:
            raise ProcessLookupError(3, "No such process")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", ending_read_text)
    monkeypatch.setattr(argon2_diagnostics, "_PROC", proc)
    stored = argon2.PasswordHasher().hash(_PASSWORD)
    with RusageWindow() as window:
        argon2.PasswordHasher().verify(stored, _PASSWORD)

    report = argon2_mismatch_report(
        password=_PASSWORD, stored_hash=stored, rusage=window
    )

    host = _sections(report)["host"]
    assert [line.split(": ", 1)[0] for line in host] == _HOST_KEYS, report
    readings = dict(line.split(": ", 1) for line in host)
    assert readings["pressure cpu"] == (
        "some avg10=41.66 avg60=1.00 avg300=2.00 total=3"
        " | full avg10=0.00 avg60=0.00 avg300=0.00 total=0"
    )
    assert readings["pressure io"].startswith("some avg10=12.50 "), report
    assert readings["vmstat"] == "pswpin=17 pswpout=23"
    assert readings["native threads"] == "python, grpc_global_tim"
    deltas = re.fullmatch(
        r"ru_majflt=\d+ ru_minflt=(\d+) ru_nivcsw=\d+ ru_nvcsw=\d+",
        readings["rusage over the refused call"],
    )
    # The verify's 64 MiB of fresh memory faults pages in, so a window that
    # measured nothing would show here.
    assert deltas and int(deltas[1]) > 0, report


def test_unreadable_host_state_does_not_break_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A report that raised would replace the failure it was meant to explain.
    def unreadable(who: int) -> resource.struct_rusage:
        raise OSError("getrusage gone")

    monkeypatch.setattr(argon2_diagnostics, "_PROC", tmp_path / "no-proc")
    monkeypatch.setattr(resource, "getrusage", unreadable)
    stored = argon2.PasswordHasher().hash(_PASSWORD)
    with RusageWindow() as window:
        pass

    report = argon2_mismatch_report(
        password=_PASSWORD, stored_hash=stored, rusage=window
    )

    host = _sections(report)["host"]
    assert [line.split(": ", 1)[0] for line in host] == _HOST_KEYS, report
    assert all(line.split(": ", 1)[1].startswith("unavailable: ") for line in host), (
        report
    )
