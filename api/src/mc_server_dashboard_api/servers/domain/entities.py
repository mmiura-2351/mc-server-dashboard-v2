"""Entity for the servers context: the :class:`Server` record.

Pure data with its at-rest policy, standard-library only. A ``Server`` is the
authoritative record of a Minecraft server (FR-SRV-3): community-scoped, with a
config blob, the desired/observed state split (FR-SRV-4), an immutable execution
backend (FR-EXE-3), and a nullable assigned Worker (FR-WRK-4). The shape mirrors
DATABASE.md Section 7.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
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
    are never an authority. ``execution_backend`` is immutable for the server's
    lifetime (FR-EXE-3). ``assigned_worker_id`` is null until placement.
    """

    id: ServerId
    community_id: CommunityId
    name: ServerName
    mc_edition: str
    mc_version: str
    server_type: ServerType
    execution_backend: ExecutionBackend
    config: dict[str, Any]
    desired_state: DesiredState
    observed_state: ObservedState
    observed_at: dt.datetime | None
    assigned_worker_id: WorkerId | None
    created_at: dt.datetime
    updated_at: dt.datetime

    def is_at_rest(self) -> bool:
        """Return whether the server is fully stopped for edits/deletion.

        At rest means the operator wants it stopped *and* the last observed state
        is one with no live working set to diverge from: ``stopped`` or the
        API-inferred ``unknown`` (the owning Worker is gone). Config/name edits
        and deletion are gated on this (Section 6.9 spirit).
        """

        return self.desired_state is DesiredState.STOPPED and self.observed_state in (
            ObservedState.STOPPED,
            ObservedState.UNKNOWN,
        )
