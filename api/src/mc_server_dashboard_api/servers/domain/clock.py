"""The servers-context ``Clock`` Port: the source of the current time.

A Port so use cases never read the wall clock directly, keeping them
deterministic and testable (TESTING.md Section 4). The servers context owns its
own ``Clock`` rather than importing another context's, to keep the domain free of
cross-context imports (ARCHITECTURE.md Section 2.1); a later refactor may promote
a single shared ``Clock`` without changing its shape.
"""

from __future__ import annotations

import abc
import datetime as dt


class Clock(abc.ABC):
    """Port: returns the current instant, always timezone-aware (UTC)."""

    @abc.abstractmethod
    def now(self) -> dt.datetime:
        """Return the current time as a timezone-aware UTC datetime."""

    @abc.abstractmethod
    def monotonic(self) -> float:
        """Return seconds on a clock that never steps, to measure a duration with.

        Only the difference between two readings means anything. A start measures
        what its wait for the Worker's uploads left of the hydrate budget on it
        (issue #3950), so a wall-clock step cannot shorten or lengthen the hydrate.
        """
