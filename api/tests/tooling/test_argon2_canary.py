"""The argon2 canary the gate runs beside the api chain (issue #3748).

scripts/argon2_canary.py runs here as the gate runs it, with this venv's
interpreter and ``-I``, so a check that disagrees, a stop and a lost parent are
each seen in the process's own log and exit status.
"""

from __future__ import annotations

import importlib.util
import os
import re
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CANARY = ROOT / "scripts" / "argon2_canary.py"

# A check is three 64 MiB computations; under a loaded gate it can take seconds.
_WAIT_S = 60.0


def _start(log: Path, *args: str) -> subprocess.Popen[bytes]:
    with log.open("wb") as out:
        return subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(CANARY),
                "--parent",
                str(os.getpid()),
                "--interval",
                "0.05",
                *args,
            ],
            stdout=out,
            stderr=subprocess.STDOUT,
        )


def _wait_for_line(log: Path, pattern: str) -> str:
    deadline = time.monotonic() + _WAIT_S
    while time.monotonic() < deadline:
        lines = log.read_text().splitlines() if log.exists() else []
        for line in lines:
            if re.search(pattern, line):
                return line
        time.sleep(0.05)
    raise AssertionError(f"no line matching {pattern!r}:\n{log.read_text()}")


def _gone(pid: int) -> bool:
    """Whether ``pid`` has exited, reaped or not."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


def test_a_tag_that_disagrees_is_a_mismatch_line_and_the_exit_is_still_0(
    tmp_path: Path,
) -> None:
    log = tmp_path / "argon2-canary.log"
    canary = _start(log, "--expected-p4", "00" * 32)
    try:
        # Check 1, not only the start-up comparison: every check compares.
        line = _wait_for_line(log, r" MISMATCH check=1 kind=p4 ")
    finally:
        canary.terminate()
        returncode = canary.wait(timeout=_WAIT_S)

    assert f" expected={'00' * 32} " in line, line
    assert returncode == 0, log.read_text()


def test_sigterm_stops_a_healthy_canary_with_its_totals(tmp_path: Path) -> None:
    log = tmp_path / "argon2-canary.log"
    canary = _start(log, "--summary-every", "1")
    try:
        _wait_for_line(log, r" summary checks=1 ")
    finally:
        canary.terminate()
        returncode = canary.wait(timeout=_WAIT_S)

    assert returncode == 0, log.read_text()
    last = log.read_text().splitlines()[-1]
    # The hard-coded answers hold here, so a healthy run counts no mismatch.
    assert re.search(
        r" stopped by SIGTERM: checks=\d+ mismatches=0 errors=0"
        r" cpu_s=\d+\.\d wall_s=\d+\.\d$",
        last,
    ), log.read_text()


def test_the_canary_does_not_outlive_its_parent(tmp_path: Path) -> None:
    # A SIGKILL of the gate runs no trap, so the canary itself notices.
    log = tmp_path / "argon2-canary.log"
    parent = subprocess.Popen(
        [
            "bash",
            "-c",
            '"$0" -I "$1" --parent $$ --interval 0.05 >"$2" 2>&1 & echo $!; wait',
            sys.executable,
            str(CANARY),
            str(log),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert parent.stdout is not None
    pid = int(parent.stdout.readline())
    try:
        _wait_for_line(log, r" start pid=")
        parent.kill()
        parent.wait(timeout=_WAIT_S)

        _wait_for_line(log, r" stopped: its parent is gone: checks=")
        deadline = time.monotonic() + _WAIT_S
        while not _gone(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _gone(pid), log.read_text()
    finally:
        if not _gone(pid):
            os.kill(pid, signal.SIGKILL)


def test_a_parent_gone_before_the_canary_starts_still_stops_it(
    tmp_path: Path,
) -> None:
    # The gate can die while the canary is still starting. Read then, its own
    # parent would already be whatever adopted it, so it watches the pid the
    # gate passed instead.
    log = tmp_path / "argon2-canary.log"
    parent = subprocess.run(
        [
            "bash",
            "-c",
            '"$0" -I "$1" --parent $$ --interval 0.05 >"$2" 2>&1 & echo $!',
            sys.executable,
            str(CANARY),
            str(log),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pid = int(parent.stdout)
    try:
        _wait_for_line(log, r" stopped: its parent is gone: checks=")
        deadline = time.monotonic() + _WAIT_S
        while not _gone(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _gone(pid), log.read_text()
    finally:
        if not _gone(pid):
            os.kill(pid, signal.SIGKILL)


class _AfterTheFirstCheck(Exception):
    pass


def test_a_start_up_computation_that_raises_is_an_error_and_the_checks_go_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # argon2 reports a failed 64 MiB allocation as an exception; the start-up
    # computation that meets one must not leave the canary idle. The canary
    # runs in this process, with its computations faked, for one check.
    spec = importlib.util.spec_from_file_location("argon2_canary", CANARY)
    assert spec and spec.loader
    canary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(canary)
    computed: list[int] = []

    def tag(lanes: int) -> bytes:
        computed.append(lanes)
        if len(computed) == 1:
            raise MemoryError("start-up")
        return bytes.fromhex(canary._EXPECTED_TAGS[lanes])

    def stop_after_the_first_check(seconds: float) -> None:
        raise _AfterTheFirstCheck

    monkeypatch.setattr(canary, "_tag", tag)
    monkeypatch.setattr(
        canary,
        "_control_readbacks",
        lambda: (canary._CONTROL_SHA256, canary._CONTROL_SHA256),
    )
    monkeypatch.setattr(
        canary,
        "time",
        types.SimpleNamespace(
            monotonic=time.monotonic, sleep=stop_after_the_first_check
        ),
    )

    with pytest.raises(_AfterTheFirstCheck):
        canary._Canary(
            interval=2.0, summary_every=1, expected=canary._EXPECTED_TAGS
        ).run()

    lines = capsys.readouterr().out.splitlines()
    assert any(" ERROR check=0 kind=p4 MemoryError('start-up')" in x for x in lines)
    assert re.search(r" summary checks=1 mismatches=0 errors=1 ", lines[-1]), lines
