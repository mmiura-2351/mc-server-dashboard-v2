#!/usr/bin/env python3
"""A clean-process argon2 canary beside the gate's api chain (issue #3748).

A correct password has intermittently failed to verify inside the api suite,
only ever during a gate: libargon2 computed a wrong tag once, and the same
process verified the same hash fine moments later. This process shares nothing
with the suite but the host. It repeats one check, then sleeps, and each check
computes:

* Argon2id at the production cost (64 MiB, 3 passes, 4 lanes on 4 threads)
  over a fixed password and salt, compared with the hard-coded tag;
* the same with 1 lane, which runs on one thread;
* a control without argon2: a 64 MiB pattern written, then read back twice.

Both tags are also computed once at start-up, as check 0. Each line it prints
starts with a UTC timestamp: a start line, a summary every --summary-every
checks, a MISMATCH line for any computation that disagrees, an ERROR line for
one that raised, and a last line when it stops. It stops, with exit status 0,
on SIGTERM or SIGINT or once the --parent pid is no longer its parent, without
waiting for the check in hand.

scripts/check_parallel.sh runs it for as long as the api chain runs, with the
api venv's interpreter, isolated (-I) and at nice 19, into
check-logs/argon2-canary.log. docs/dev/AGENTS.md Section 3 says how to read it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import resource
import signal
import sys
import threading
import time
from collections.abc import Callable
from typing import NoReturn

from argon2.low_level import Type, hash_secret_raw

_STOP_SIGNALS = {signal.SIGTERM, signal.SIGINT}

_PASSWORD = b"Wm7!qz#Lp2vT"
_SALT = bytes(range(16))
# Argon2id at argon2-cffi's default cost, which production uses (RFC 9106's
# low-memory profile); the lanes vary per check.
_EXPECTED_TAGS = {
    4: "3a73db5dfa2f4e4180444c80596491263ffd69ae0204e1c4ac9aa88db735f9cb",
    1: "9588ea70ba5fb5b11ecd1cee3b10b96b7b4548a41c1f09713189f2225182f552",
}

# Each 4 KiB page of the control holds its own index, so a page read back from
# the wrong place disagrees too.
_PAGE = 4096
_CONTROL_PAGES = 64 * 1024 * 1024 // _PAGE
_CONTROL_SHA256 = "2336ada830e92f6e61f8816e50d546cb9c1317a797f377d70b87e5d6e44f475e"


def _tag(lanes: int) -> bytes:
    return hash_secret_raw(
        _PASSWORD,
        _SALT,
        time_cost=3,
        memory_cost=65536,
        parallelism=lanes,
        hash_len=32,
        type=Type.ID,
    )


def _control_readbacks() -> tuple[str, str]:
    """Write the control pattern, then read it back twice, as digests."""
    pattern = bytearray(_CONTROL_PAGES * _PAGE)
    for page in range(_CONTROL_PAGES):
        index = page.to_bytes(8, "little")
        pattern[page * _PAGE : (page + 1) * _PAGE] = index * (_PAGE // 8)
    return hashlib.sha256(pattern).hexdigest(), hashlib.sha256(pattern).hexdigest()


class _Canary:
    def __init__(
        self, *, interval: float, summary_every: int, expected: dict[int, str]
    ) -> None:
        self._interval = interval
        self._summary_every = summary_every
        self._expected = expected
        self._started = time.monotonic()
        self._lock = threading.Lock()
        self._slowest: dict[str, float] = {}
        self._checks = self._mismatches = self._errors = 0

    def _write(self, text: str) -> None:
        stamp = dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")
        sys.stdout.write(f"{stamp} {text}\n")
        sys.stdout.flush()

    def _emit(self, text: str) -> None:
        with self._lock:
            self._write(text)

    def _totals(self) -> str:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return (
            f"checks={self._checks} mismatches={self._mismatches}"
            f" errors={self._errors} cpu_s={usage.ru_utime + usage.ru_stime:.1f}"
            f" wall_s={time.monotonic() - self._started:.1f}"
        )

    def _compute(self, check: int, kind: str, compute: Callable[[], str]) -> None:
        """Run one computation; ``compute`` names a disagreement, or returns ''."""
        started = time.monotonic()
        try:
            disagreement = compute()
        except Exception as exc:  # the outcome is the datum; keep checking
            self._errors += 1
            self._emit(f"ERROR check={check} kind={kind} {exc!r}")
            return
        seconds = time.monotonic() - started
        self._slowest[kind] = max(self._slowest.get(kind, 0.0), seconds)
        if disagreement:
            self._mismatches += 1
            self._emit(
                f"MISMATCH check={check} kind={kind} {disagreement}"
                f" seconds={seconds:.3f}"
            )

    def run(self) -> None:
        self._emit(
            f"start pid={os.getpid()} interval_s={self._interval}"
            f" summary_every={self._summary_every}"
        )

        def argon2_check(lanes: int) -> str:
            tag = _tag(lanes).hex()
            if tag == self._expected[lanes]:
                return ""
            return f"tag={tag} expected={self._expected[lanes]}"

        def control_check() -> str:
            first, second = _control_readbacks()
            if first == second == _CONTROL_SHA256:
                return ""
            return f"first={first} second={second} expected={_CONTROL_SHA256}"

        for lanes in self._expected:
            self._compute(0, f"p{lanes}", lambda: argon2_check(lanes))
        while True:
            check = self._checks + 1
            self._compute(check, "p4", lambda: argon2_check(4))
            self._compute(check, "p1", lambda: argon2_check(1))
            self._compute(check, "control", control_check)
            self._checks = check
            if check % self._summary_every == 0:
                slowest = ",".join(f"{k}:{s:.3f}" for k, s in self._slowest.items())
                self._emit(f"summary {self._totals()} slowest_s={slowest}")
                self._slowest.clear()
            time.sleep(self._interval)

    def stop(self, reason: str) -> NoReturn:
        """Print the last line and exit at once, whatever the checks are doing."""
        with self._lock:  # held to the end: no line can follow this one
            self._write(f"{reason}: {self._totals()}")
            os._exit(0)


def main(argv: list[str] | None = None) -> NoReturn:
    # Blocked in every thread, so the signals wait for sigtimedwait below
    # rather than kill the process mid-line; the checks' threads inherit this.
    signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--parent",
        type=int,
        required=True,
        metavar="PID",
        help="the gate's pid: the canary stops once that is no longer its parent",
    )
    parser.add_argument("--interval", type=float, default=2.0, metavar="SECONDS")
    parser.add_argument("--summary-every", type=int, default=30, metavar="CHECKS")
    parser.add_argument(
        "--expected-p4",
        default=_EXPECTED_TAGS[4],
        metavar="HEX",
        help="the 4-lane tag to expect; a test passes a wrong one",
    )
    args = parser.parse_args(argv)
    canary = _Canary(
        interval=args.interval,
        summary_every=args.summary_every,
        expected={4: args.expected_p4, 1: _EXPECTED_TAGS[1]},
    )
    threading.Thread(target=canary.run, name="checks", daemon=True).start()
    while True:
        # A SIGKILL of the gate runs no trap. The gate passes its pid because
        # it may die before this starts, when os.getppid() would already name
        # whatever adopted the canary.
        if os.getppid() != args.parent:
            canary.stop("stopped: its parent is gone")
        received = signal.sigtimedwait(_STOP_SIGNALS, 1.0)
        if received is not None:
            canary.stop(f"stopped by {signal.Signals(received.si_signo).name}")


if __name__ == "__main__":
    main()
