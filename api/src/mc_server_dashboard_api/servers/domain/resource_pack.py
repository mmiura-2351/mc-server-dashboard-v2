"""Entity and value objects for resource pack management.

A :class:`ResourcePack` is the metadata of a globally uploaded resource pack.
Resource packs are global (not community-scoped) and live in a top-level
``resource-packs/<pack-id>/`` namespace in object storage. The pack bytes live
behind the :class:`ResourcePackStore` seam; this entity only indexes them.

A :class:`ResourcePackAssignment` links a resource pack to a server (one
assignment per server at most), carrying the ``require_resource_pack`` flag
and an optional ``resource_pack_prompt``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.value_objects import ServerId


@dataclass(frozen=True)
class ResourcePackId:
    """The identity of a :class:`ResourcePack` (a UUID primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> ResourcePackId:
        """Generate a fresh, random resource pack id."""

        return cls(uuid.uuid4())


@dataclass
class ResourcePack:
    """Row of the ``resource_packs`` table (migration 0018).

    ``filename`` is the original upload filename. ``sha1_hash`` and
    ``sha256_hash`` are the hex-encoded checksums of the pack bytes at upload
    time. ``uploaded_by`` is the acting user (a plain UUID, no FK so the row
    survives the user's deletion).
    """

    id: ResourcePackId
    filename: str
    display_name: str
    description: str | None
    sha1_hash: str
    sha256_hash: str
    size_bytes: int
    uploaded_by: uuid.UUID
    created_at: dt.datetime
    updated_at: dt.datetime


@dataclass
class ResourcePackAssignment:
    """Row of the ``server_resource_pack_assignments`` table (migration 0018).

    Links a server to a resource pack. At most one assignment per server
    (``server_id`` is the PK). ``require_resource_pack`` controls whether the
    server forces the pack on clients; ``resource_pack_prompt`` is an optional
    message shown to clients.
    """

    server_id: ServerId
    resource_pack_id: ResourcePackId
    require_resource_pack: bool
    resource_pack_prompt: str | None
    assigned_by: uuid.UUID
    created_at: dt.datetime
    updated_at: dt.datetime
