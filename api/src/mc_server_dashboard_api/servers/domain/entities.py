"""Entity for the servers context: the :class:`Server` record.

Pure data with its at-rest policy, standard-library only. A ``Server`` is the
authoritative record of a Minecraft server (FR-SRV-3): community-scoped, with a
config blob, the desired/observed state split (FR-SRV-4), and a nullable
assigned Worker (FR-WRK-4). The shape mirrors
DATABASE.md Section 7.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from mc_server_dashboard_api.servers.domain.errors import (
    ServerAssignedError,
    ServerError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    NOT_RESTARTED_CRASH_REASONS,
    CommunityId,
    CrashReason,
    DesiredState,
    FinalSnapshotRefusal,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)


@dataclass
class Server:
    """Row of the ``server`` table (DATABASE.md Section 7).

    ``config`` is the configuration blob (properties, JVM args, snapshot-interval
    override per FR-DATA-7). ``desired_state`` is the source of truth for intent;
    ``observed_state`` + ``observed_at`` are a cache of the last Worker report and
    are never an authority. ``assigned_worker_id`` is null until placement.
    """

    id: ServerId
    community_id: CommunityId
    name: ServerName
    mc_edition: str
    mc_version: str
    server_type: ServerType
    config: dict[str, Any]
    desired_state: DesiredState
    observed_state: ObservedState
    observed_at: dt.datetime | None
    assigned_worker_id: WorkerId | None
    created_at: dt.datetime
    updated_at: dt.datetime
    # The tracked Minecraft game port (issue #243), assigned at create from the
    # configured range and unique deployment-wide. ``None`` for legacy/imported
    # rows that predate port tracking. Defaulted so existing constructions (tests,
    # other use cases) need not pass it; the create flow sets it explicitly.
    game_port: int | None = None
    # The public Bedrock UDP port (issue #1541), allocated from the dedicated UDP
    # window when Geyser is detected among the server's plugins and released on
    # Geyser uninstall. ``None`` is the not-Bedrock-enabled state -- non-None *is*
    # Bedrock-enabled, there is no separate boolean. Unique deployment-wide.
    bedrock_port: int | None = None
    # The relay slug (issue #955): a DNS-label string unique deployment-wide,
    # auto-generated at create and renameable via the server update PATCH.
    # Defaults to empty string here so existing test constructions that predate
    # the slug column need not pass it; the create flow always assigns a real slug.
    slug: str = ""
    # The scheduled-backup retention policy (issue #1841): the raw jsonb value
    # (``{"keep_last": N}`` or ``{"daily": D, "weekly": W, "monthly": M}``), or
    # ``None`` when no retention is configured. Kept raw here — like ``config`` —
    # so a malformed persisted value cannot fail every entity load; consumers
    # parse it via ``RetentionPolicy.from_json`` at the use site.
    backup_retention: dict[str, Any] | None = None
    # The last crash's reason and detail as the Worker reported them (issue
    # #3281), stored by the write that records ``crashed`` and cleared by the
    # next write of any other observed state, so both are ``None`` unless the
    # server reads ``crashed``. ``crash_reason`` is ``None`` for an unclassified
    # crash too; ``crash_detail`` is bounded (``bound_status_detail``).
    crash_reason: CrashReason | None = None
    crash_detail: str | None = None
    # Whether the assigned Worker has yet to hydrate the server under this
    # assignment (issue #3890). The repository sets it with a start's placement
    # and clears it with the release; the start clears it once that Worker holds
    # the store's working set, and a ``running`` report clears it too. While it
    # is set, the Worker's scratch may be older than the store, so nothing
    # publishes a snapshot from it.
    awaiting_hydrate: bool = False
    # Why the assigned Worker refused the stop's final snapshot (issue #4054),
    # or ``None``. The repository writes it only while the row is
    # ``desired_state=stopped`` and still assigned to that Worker, and clears it
    # with the assignment, so it is ``None`` on every unassigned row.
    final_snapshot_refusal: FinalSnapshotRefusal | None = None

    def observe(self, state: ObservedState, at: dt.datetime) -> None:
        """Reflect an observed-state write that landed (issues #292, #3281).

        The state and its stamp, and the stored last crash the write keeps for
        ``crashed`` and clears for any other state, so the entity a use case
        returns reads like the row it wrote.
        """

        self.observed_state = state
        self.observed_at = at
        if state is not ObservedState.CRASHED:
            self.crash_reason = None
            self.crash_detail = None

    def crash_not_restarted(self) -> bool:
        """Whether the reconciler leaves this crash alone (issue #3281).

        The server is crashed under a running intent (a stopped one is owed no
        restart), the crash's reason is one the next attempt fails the same way
        (:data:`NOT_RESTARTED_CRASH_REASONS`), and it was reported after the
        last write to the row (``observed_at > updated_at``). ``updated_at`` is
        bumped by every row write, not only an intent commit: an operator start,
        and also an edit, a rename, a Geyser install or removal that moves the
        Bedrock port, the Bedrock gate-flip sweep, and a restart the Worker then
        refuses. Each such write re-arms one restart, which fails the same way
        and is reported again, so it costs one more attempt, never a loop. The
        start case is what this protects: after a stop and start the row reads
        ``crashed`` with the old reason until the Worker reports the new launch,
        and a start whose answer was lost there is owed a replay.
        """

        return (
            self.desired_state is DesiredState.RUNNING
            and self.observed_state is ObservedState.CRASHED
            and self.crash_reason in NOT_RESTARTED_CRASH_REASONS
            and self.observed_at is not None
            and self.observed_at > self.updated_at
        )

    def is_at_rest(self) -> bool:
        """Return whether the server is fully stopped for edits/deletion.

        At rest means the operator wants it stopped *and* the last observed state
        is one with no live working set to diverge from: ``stopped``,
        ``unknown``, or ``crashed`` (the process died, so there is no live
        working set — strictly safer than ``unknown``, where the Worker may still
        hold a live instance we cannot see). Reads of the store and deletion are
        gated on this (Section 6.9 spirit); an API-side write to the working set
        gates on :meth:`is_quiescent`, which also wants the server released.

        ``unknown`` has two provenances and the caveat above covers both.
        API-inferred: the owning Worker is gone, so nobody can speak for the
        instance. Worker-asserted (issue #2475): the Worker holds a failed-stop
        orphan and its backend daemon cannot answer whether the container is
        alive. The second is the same claim made from closer up — the Worker
        knows it holds an instance and cannot confirm it dead — so it is if
        anything a stronger reason to read ``unknown`` as "possibly still live".

        It stays in the at-rest set anyway, because this predicate is not what
        protects the world. The owning Worker's own reservation guard is: while
        it has a failed-stop orphan recorded for the id, that Worker refuses
        start, hydrate, stopped-id snapshot and scratch reclamation for it, so
        nothing can overwrite or capture a possibly-live working set whatever
        this returns. What this gates is API-side reading, editing and deletion
        of the row, and excluding ``unknown`` would wedge those forever for a
        server whose Worker is gone — nothing will ever report a terminal state
        for it.
        """

        return self.desired_state is DesiredState.STOPPED and self.observed_state in (
            ObservedState.STOPPED,
            ObservedState.UNKNOWN,
            ObservedState.CRASHED,
        )

    def is_quiescent(self) -> bool:
        """Return whether no Worker holds the server at rest any more (#3998).

        At rest *and* unassigned. A stop keeps its Worker assigned until the final
        snapshot settles (#869), and that snapshot publishes over the store: the
        publish guard lets the assigned Worker through even behind an API
        sentinel ``current`` (the #1703 exemption, kept for adoption, #3865). So an
        API-side write to the working set made while the row is at rest but still
        assigned is overwritten. Every such write gates on this predicate, not on
        :meth:`is_at_rest`; reads, which write nothing, may keep the latter. A
        write made once quiescent can still be published over by a later start
        that adopts a live instance from before it (#4004).
        """

        return self.assigned_worker_id is None and self.is_at_rest()

    def not_quiescent_error(self, not_at_rest: type[ServerError]) -> ServerError:
        """The error a write door refuses a server that is not quiescent with.

        The door's own ``not_at_rest`` error while the server is not at rest, so
        each door keeps the reason it has always answered; otherwise the server is
        at rest but still assigned, and the refusal is :class:`ServerAssignedError`
        (409 ``server_assigned``): the operator retries once the stop releases it.
        """

        if not self.is_at_rest():
            return not_at_rest(str(self.id.value))
        return ServerAssignedError(str(self.id.value))
