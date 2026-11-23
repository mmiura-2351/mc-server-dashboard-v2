"""Per-server record of the last stop dispatch the Worker REFUSED (issue #2478).

The reconciler waits ``grace_seconds`` (660s) before replaying a stale stop. That
wait exists for ONE reason on this path — the #930 floor
``grace_seconds > control.stop_timeout_seconds``: while the first dispatch is in
flight the row stays diverged, so without the wait the reconciler would re-select
and re-send the same stop before the first round trip settles. The grace is not
"a stop takes a while to be right"; it is "the previous dispatch may still be
running".

**The row cannot tell the two apart.** A stop whose dispatch the Worker refused
and a stop whose dispatch never happened (the API died between the intent commit
and the dispatch, ``lifecycle.py``'s stale-intent window) leave *identical* rows:
``desired=stopped``, ``observed=running``, still assigned, ``updated_at`` at the
commit, ``observed_at`` at the last Worker report. The ``server`` table records no
dispatch attempt and no dispatch outcome (``adapters/models.py``), and it must
not: ``observed_state``/``observed_at`` are a cache of Worker REPORTS, and issue
#2476 removed the one place the API manufactured an observed state out of a
refusal. So the distinction is carried here instead of on the row.

Held in memory, deliberately. The knowledge is "*this process* dispatched and the
Worker answered", and it is lost exactly when that process dies — which is
precisely the case the full grace exists for (a dispatch that may never have been
sent, or whose response was lost). A restart therefore falls back to the full
grace, which is the conservative answer, and no persisted marker can go stale
across one. Same rationale as the reconciler's in-memory backoff map and the
registry's held-working-set inventory.

An ``EXPIRED`` refusal is recorded as such (issue #3682). The Worker answers it in
time only once the server's lane frees, usually as the command ahead of it reports,
so a Worker report lands with the refusal. ``EXPIRED`` proves the stop was never
applied, so the reconciler does not let a later ``running`` report supersede it.
An ``EXPIRED`` answer that arrives after the dispatch timed out never reaches here:
the late result is dropped, and the stop keeps the full grace.

Size: one entry per server at most — every stop dispatch and every final-snapshot
dispatch clears the entry (:meth:`forget`, before the attempt), and a refused stop
replaces it. It is NOT bounded by the live fleet, though. A refusal superseded by a
Worker report can converge through ``clear_stale_assignment`` with no dispatch at
all (its disconnected-Worker legs take no final snapshot), and deleting that server
leaves its entry behind: nothing evicts here on delete, unlike the tunnel table
(#1544). The real bound is one entry per server that had a refused stop during this
process's lifetime. Left that way deliberately — a
leftover entry is unreachable, because the reconciler only ever looks up ids
``list_reconcilable`` returned and a deleted row is never among them, and server ids
are never reused. An eviction hook would buy a few dozen bytes for a use-case
dependency this module does not otherwise need.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


@dataclass(frozen=True)
class _Refusal:
    at: dt.datetime
    expired: bool


@dataclass
class StopDispatchRefusals:
    """When each server's last stop dispatch was refused, if it was, and whether
    that refusal was ``EXPIRED``."""

    _refusals: dict[ServerId, _Refusal] = field(default_factory=dict)

    def forget(self, server_id: ServerId) -> None:
        """Drop any recorded verdict, because a new dispatch is being attempted.

        Called immediately BEFORE every stop dispatch, so a recorded refusal only
        ever describes the LATEST attempt. Without it a dispatch that timed out —
        the API gave up waiting, but the Worker may still be executing the stop —
        would inherit the previous attempt's "settled" verdict and hand the
        reconciler a short grace for a command that really may be in flight, which
        is the #930 floor's own failure mode.

        Also called before every final-snapshot dispatch: the process is gone by
        then, so the refused stop no longer describes the row (issue #3682).
        """

        self._refusals.pop(server_id, None)

    def record_refusal(
        self, server_id: ServerId, *, outcome: CommandOutcome, at: dt.datetime
    ) -> None:
        """Record ``outcome`` as the settled verdict of a stop dispatch at ``at``.

        ``at`` is the instant the API read the Worker's answer (issue #4117), so
        a report read after the answer supersedes the verdict, however long the
        answer waited to be handled.

        Recorded only when the outcome PROVES nothing is in flight for the server:

        * The Worker returned a result at all. A returned result is the end of the
          round trip the #930 floor budgets, so that dispatch is settled, not
          running. A timeout or a disconnect never reaches here (the seam raises
          ``WorkerUnavailableError`` instead of answering), and that is the point:
          those are exactly the outcomes where the command may still be executing.
        * The result is not ``BUSY``. ``BUSY`` is the Worker's reservation guard
          reporting that ANOTHER mutating command is already in flight for the id
          (issue #824) — after #2475/#2476 typically its own converger resolving a
          failed-stop orphan. This dispatch settled, but something else has not, so
          the property does not hold; the converger's cadence owns that case and a
          reconciler retry would only collect another ``BUSY`` (#2478).

        A successful outcome is not a refusal and is likewise not recorded; the
        caller's own success paths converge the row out of the reconciler's reach.

        Whether the refusal was ``EXPIRED`` is kept with it (issue #3682): that
        answer proves the stop was never applied, which lets the reconciler keep the
        short grace past a Worker report that arrives with it.
        """

        if outcome.success or outcome.status is CommandStatus.BUSY:
            return
        self._refusals[server_id] = _Refusal(
            at=at, expired=outcome.status is CommandStatus.EXPIRED
        )

    def refused_at(self, server_id: ServerId) -> dt.datetime | None:
        """Return when this server's last stop dispatch was refused, else ``None``."""

        refusal = self._refusals.get(server_id)
        return None if refusal is None else refusal.at

    def expired(self, server_id: ServerId) -> bool:
        """Return whether this server's last stop dispatch was refused ``EXPIRED``."""

        refusal = self._refusals.get(server_id)
        return refusal is not None and refusal.expired
