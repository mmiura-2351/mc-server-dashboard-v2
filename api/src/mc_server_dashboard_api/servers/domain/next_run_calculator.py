"""Cron next-occurrence Port for the general scheduler (epic #649, issue #1835).

Cron parsing and next-occurrence math need a cron engine, which the stdlib-only
domain must not import (ARCHITECTURE.md Section 2.1); this Port is that seam.
The concrete adapter wraps ``cronsim``
(``servers/adapters/cronsim_next_run_calculator.py``). Interval cadences never
pass through here — their math is pure and lives in :mod:`.schedule`.
"""

from __future__ import annotations

import abc
import datetime as dt


class NextRunCalculator(abc.ABC):
    """Port: validate cron expressions and compute the next occurrence."""

    @abc.abstractmethod
    def validate(self, expr: str) -> None:
        """Raise ``InvalidCronExpressionError`` unless ``expr`` is a valid
        5-field cron expression.

        The write-path gate: the CRUD use case validates before a cron cadence
        is persisted, so ``next_after`` only ever sees expressions that parse.
        """

    @abc.abstractmethod
    def next_after(self, expr: str, tz: str, after: dt.datetime) -> dt.datetime:
        """Return the first occurrence of ``expr`` strictly after ``after``, UTC.

        ``expr`` is evaluated on local wall-clock time in the IANA zone ``tz``
        (already zoneinfo-validated by the ``Schedule`` entity), with sane DST
        behavior: a nonexistent local time (spring forward) fires right after
        the gap, a repeated local time (fall back) fires once. ``after`` is any
        timezone-aware instant; the result is timezone-aware UTC.
        """
