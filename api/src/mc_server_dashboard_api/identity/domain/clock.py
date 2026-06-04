"""The ``Clock`` Port: the source of the current time for the domain.

A Port so use cases never read the wall clock directly, keeping them
deterministic and testable (TESTING.md Section 4). The interface lives in the
identity context as its first consumer (tokens, FR-AUTH-2); a later context that
needs time can promote it to a shared location without changing its shape.
"""

from __future__ import annotations

import abc
import datetime as dt


class Clock(abc.ABC):
    """Port: returns the current instant, always timezone-aware (UTC)."""

    @abc.abstractmethod
    def now(self) -> dt.datetime:
        """Return the current time as a timezone-aware UTC datetime."""
