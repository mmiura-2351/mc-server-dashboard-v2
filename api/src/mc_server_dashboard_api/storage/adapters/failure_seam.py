"""Crash-injection seam for the fs adapter (testing only).

STORAGE.md Section 4.3 requires the atomic-publish invariant to hold across a
crash at *every* step (stage / move-into-snapshots / symlink-flip / parent-fsync /
reclaim). To exercise those points deterministically the adapter calls
:meth:`FailureSeam.reach` with a named phase at each boundary; the default seam
does nothing, and a test seam can be configured to raise at one named phase to
simulate a process kill there.

This lives in ``adapters`` (not ``domain``): the phase names are filesystem-
mechanism details of the fs adapter, not part of the backend-neutral Port
contract. Production wiring uses the no-op seam.
"""

from __future__ import annotations

import enum


class PublishPhase(enum.Enum):
    """Named boundaries in the atomic-publish / single-file-write mechanics.

    The values line up with the STORAGE.md Section 4.3 crash-point table so a test
    can name exactly where to inject a kill.
    """

    # Whole-working-set publish (Section 4.1/4.2).
    AFTER_STAGE = "after_stage"  # staged copy written, before move into snapshots/
    AFTER_MOVE = "after_move"  # moved into snapshots/<id>/, before the symlink flip
    AFTER_FLIP = "after_flip"  # current flipped, before parent-dir fsync
    AFTER_FSYNC = "after_fsync"  # parent fsynced, before reclaiming the old snapshot
    # Single-file write (Section 4.4).
    AFTER_VERSION_CAPTURE = (
        "after_version_capture"  # prior content saved, before overwrite
    )
    AFTER_FILE_TEMP_WRITE = (
        "after_file_temp_write"  # temp sibling written+fsynced, before rename
    )
    # Version capture (Section 5): temp copy fsynced, before rename into versions/.
    AFTER_VERSION_TEMP_WRITE = (
        "after_version_temp_write"  # version temp fsynced, before rename
    )


class InjectedCrash(Exception):
    """Simulates a process kill at a publish phase (test seam only)."""


class FailureSeam:
    """No-op seam: reaching a phase does nothing (production default)."""

    def reach(self, phase: PublishPhase) -> None:  # noqa: D401 - simple hook
        """Hook invoked by the adapter at ``phase``; the no-op seam ignores it."""


class CrashAt(FailureSeam):
    """Test seam: raise :class:`InjectedCrash` the first time ``phase`` is reached."""

    def __init__(self, phase: PublishPhase) -> None:
        self._phase = phase
        self._fired = False

    def reach(self, phase: PublishPhase) -> None:
        if phase is self._phase and not self._fired:
            self._fired = True
            raise InjectedCrash(f"injected crash at {phase.value}")
