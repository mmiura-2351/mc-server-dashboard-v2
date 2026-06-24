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

from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
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
    # The relay slug (issue #955): a DNS-label string unique deployment-wide,
    # auto-generated at create and renameable via the server update PATCH.
    # Defaults to empty string here so existing test constructions that predate
    # the slug column need not pass it; the create flow always assigns a real slug.
    slug: str = ""

    def is_at_rest(self) -> bool:
        """Return whether the server is fully stopped for edits/deletion.

        At rest means the operator wants it stopped *and* the last observed state
        is one with no live working set to diverge from: ``stopped``, the
        API-inferred ``unknown`` (the owning Worker is gone), or ``crashed`` (the
        process died, so there is no live working set — strictly safer than
        ``unknown``, where the Worker may still hold a live instance we cannot
        see). Config/name edits and deletion are gated on this (Section 6.9
        spirit).
        """

        return self.desired_state is DesiredState.STOPPED and self.observed_state in (
            ObservedState.STOPPED,
            ObservedState.UNKNOWN,
            ObservedState.CRASHED,
        )
