"""Domain for player groups (OP / whitelist) attached to servers (issue #276).

A :class:`PlayerGroup` is a community-scoped, reusable list of players of one
:class:`GroupKind` (``op`` or ``whitelist``). A group is attached to many servers
and a server may carry many groups; attaching a group makes its players part of
the authoritative ``ops.json`` (kind ``op``) or ``whitelist.json`` (kind
``whitelist``) the next time that server's files are regenerated.

Pure data + the file-rendering rules, standard-library only (the servers domain
imports no framework and no other context, ARCHITECTURE.md Section 2.1). The ids
mirror DATABASE.md's uuid PKs; ``CommunityId`` is the existing by-id-value foreign
reference from :mod:`~mc_server_dashboard_api.servers.domain.value_objects`.

The rendered JSON schemas are exactly Minecraft's (issue #276):

- ``ops.json``: a list of ``{"uuid", "name", "level", "bypassesPlayerLimit"}``
  objects (operator level defaults to :data:`DEFAULT_OP_LEVEL` = 4).
- ``whitelist.json``: a list of ``{"uuid", "name"}`` objects.

Across several groups attached to one server, the player sets are **union-merged**
and rendered in a deterministic order (sorted by uuid) so the generated file is
stable diff-to-diff regardless of attach order or group iteration order.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from uuid import UUID

from mc_server_dashboard_api.servers.domain.errors import (
    InvalidGroupNameError,
    InvalidPlayerError,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId

# Minecraft's default operator level for a newly opped player (issue #276). Stored
# in every ops.json entry; not configurable in this slice (a constant is the
# smallest honest choice — document if a per-player level is ever needed).
DEFAULT_OP_LEVEL = 4

# The authoritative working-set file each group kind regenerates.
OPS_FILE = "ops.json"
WHITELIST_FILE = "whitelist.json"


class GroupKind(enum.Enum):
    """Whether a group feeds ops.json or whitelist.json (DATABASE.md CHECK enum)."""

    OP = "op"
    WHITELIST = "whitelist"

    @property
    def target_file(self) -> str:
        """The working-set file this kind regenerates."""

        return OPS_FILE if self is GroupKind.OP else WHITELIST_FILE


@dataclass(frozen=True)
class GroupId:
    """The identity of a :class:`PlayerGroup` (a uuid primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> GroupId:
        """Generate a fresh, random group id."""

        return cls(uuid.uuid4())


@dataclass(frozen=True)
class GroupName:
    """A group's name, unique within its community + kind (DATABASE.md).

    Surrounding whitespace is trimmed and a blank name is rejected; uniqueness is
    enforced by the schema's ``UNIQUE(community_id, kind, name)``.
    """

    value: str

    def __init__(self, value: str) -> None:
        trimmed = value.strip()
        if not trimmed:
            raise InvalidGroupNameError("group name must not be blank")
        object.__setattr__(self, "value", trimmed)


@dataclass(frozen=True)
class Player:
    """One player in a group: a Minecraft uuid + username.

    The ``uuid`` is the player's account uuid (the stable key Minecraft's
    ops.json / whitelist.json record); ``username`` is the display name. A blank
    username is rejected. Membership is keyed by ``uuid`` (the upsert key), so two
    players with the same uuid are the same player regardless of username.
    """

    uuid: UUID
    username: str

    def __init__(self, player_uuid: UUID, username: str) -> None:
        trimmed = username.strip()
        if not trimmed:
            raise InvalidPlayerError("player username must not be blank")
        object.__setattr__(self, "uuid", player_uuid)
        object.__setattr__(self, "username", trimmed)


@dataclass
class PlayerGroup:
    """A reusable, community-scoped player list of one kind (issue #276).

    ``players`` is keyed by uuid: :meth:`upsert_player` adds a new player or
    updates an existing one's username, and :meth:`remove_player` deletes by uuid.
    The kind is immutable for the group's lifetime (it determines which file the
    group feeds); name and players are mutable.
    """

    id: GroupId
    community_id: CommunityId
    name: GroupName
    kind: GroupKind
    players: list[Player]

    def upsert_player(self, player: Player) -> None:
        """Add ``player`` or replace the username of an existing same-uuid entry."""

        for index, existing in enumerate(self.players):
            if existing.uuid == player.uuid:
                self.players[index] = player
                return
        self.players.append(player)

    def remove_player(self, player_uuid: uuid.UUID) -> bool:
        """Remove the player with ``player_uuid``; return whether one was removed."""

        for index, existing in enumerate(self.players):
            if existing.uuid == player_uuid:
                del self.players[index]
                return True
        return False


def merge_players(groups: list[PlayerGroup]) -> list[Player]:
    """Union-merge the players of ``groups``, deterministically ordered by uuid.

    Groups attached to one server are merged into a single player set: a player in
    two groups appears once. The order is by uuid string so the generated file is
    byte-stable regardless of attach/iteration order (issue #276). On a uuid
    collision across groups the first occurrence (in the input order) wins — the
    username is a display detail and either is acceptable; first-wins keeps the
    merge total and deterministic given a deterministic group order.
    """

    by_uuid: dict[uuid.UUID, Player] = {}
    for group in groups:
        for player in group.players:
            by_uuid.setdefault(player.uuid, player)
    return sorted(by_uuid.values(), key=lambda p: str(p.uuid))


def render_ops_json(players: list[Player]) -> list[dict[str, object]]:
    """Render the ops.json entry list for ``players`` (exact MC schema, issue #276)."""

    return [
        {
            "uuid": str(player.uuid),
            "name": player.username,
            "level": DEFAULT_OP_LEVEL,
            "bypassesPlayerLimit": False,
        }
        for player in players
    ]


def render_whitelist_json(players: list[Player]) -> list[dict[str, object]]:
    """Render the whitelist.json entry list for ``players`` (exact MC schema)."""

    return [{"uuid": str(player.uuid), "name": player.username} for player in players]
