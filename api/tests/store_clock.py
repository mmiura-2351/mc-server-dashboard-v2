"""Controllable write time for cache and object-store contract tests."""

from __future__ import annotations

import datetime as dt


class StoreClock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self) -> None:
        self.now += dt.timedelta(days=1)
