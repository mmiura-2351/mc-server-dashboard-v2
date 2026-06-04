"""The audit-context ``Clock`` Port: the source of the event time.

A Port so the writer never reads the wall clock directly, keeping it testable
(TESTING.md Section 4). The audit context owns its own ``Clock`` rather than
importing another context's, to keep the domain free of cross-context imports
(ARCHITECTURE.md Section 2.1) -- the same pattern the other contexts follow.
"""

from __future__ import annotations

import abc
import datetime as dt


class Clock(abc.ABC):
    """Port: returns the current instant, always timezone-aware (UTC)."""

    @abc.abstractmethod
    def now(self) -> dt.datetime:
        """Return the current time as a timezone-aware UTC datetime."""
