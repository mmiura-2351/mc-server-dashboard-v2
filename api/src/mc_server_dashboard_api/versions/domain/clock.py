"""The versions-context ``Clock`` Port: the source of the current time.

A Port so the JAR-pool GC (issue #293) never reads the wall clock directly,
keeping its safety-window logic deterministic and testable (TESTING.md Section 4).
The versions context owns its own ``Clock`` rather than importing another
context's, to keep the domain free of cross-context imports (the servers context's
precedent, ARCHITECTURE.md Section 2.1).
"""

from __future__ import annotations

import abc
import datetime as dt


class Clock(abc.ABC):
    """Port: returns the current instant, always timezone-aware (UTC)."""

    @abc.abstractmethod
    def now(self) -> dt.datetime:
        """Return the current time as a timezone-aware UTC datetime."""
