"""The community-context ``Clock`` Port: the source of the current time.

A Port so use cases never read the wall clock directly, keeping them
deterministic and testable (TESTING.md Section 4). The community context owns its
own ``Clock`` rather than importing the identity one, to keep the domain free of
cross-context imports (DATABASE.md Section 5); a later refactor may promote a
single shared ``Clock`` without changing its shape.
"""

from __future__ import annotations

import abc
import datetime as dt


class Clock(abc.ABC):
    """Port: returns the current instant, always timezone-aware (UTC)."""

    @abc.abstractmethod
    def now(self) -> dt.datetime:
        """Return the current time as a timezone-aware UTC datetime."""
