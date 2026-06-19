"""Entity and value objects for the global mod library.

A :class:`Mod` is the metadata of a globally uploaded mod jar. Mods are global
(not community-scoped) and live in a top-level ``mods/<mod-id>/`` namespace in
object storage. The jar bytes live behind the :class:`ModStore` seam; this
entity only indexes them.

``ModSide`` distinguishes where a mod is deployed (server ``mods/`` vs the client
bulk-download); ``ModLoader`` selects the loader family; ``ModSource`` records the
ingest path. These are the foundation only -- manifest parsing, upload, and
server assignment land in later sub-issues of epic #1258.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Literal

ModSide = Literal["server", "client", "both"]
ModLoader = Literal["fabric", "forge", "neoforge", "quilt", "paper"]
ModSource = Literal["local", "modrinth"]


@dataclass(frozen=True)
class ModId:
    """The identity of a :class:`Mod` (a UUID primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> ModId:
        """Generate a fresh, random mod id."""

        return cls(uuid.uuid4())


@dataclass
class Mod:
    """Row of the ``mods`` table (migration 0019).

    ``filename`` is the original upload filename. ``mod_identifier`` is the
    manifest mod id used for dependency-satisfaction matching; ``provides`` lists
    extra ids the jar provides. ``mc_versions`` and ``dependencies`` are parsed
    from the manifest (in a later sub-issue). ``sha256_hash`` is the hex-encoded
    content address (deduped via a unique index); ``sha512_hash`` is the
    Modrinth-published digest when imported. ``uploaded_by`` is the acting user
    (a plain UUID, no FK so the row survives the user's deletion).
    """

    id: ModId
    filename: str
    display_name: str
    description: str | None
    loader_type: ModLoader
    mod_identifier: str
    provides: list[str]
    version_number: str
    mc_versions: list[str]
    side: ModSide
    dependencies: list[dict[str, object]]
    sha256_hash: str
    sha512_hash: str | None
    size_bytes: int
    source: ModSource
    source_project_id: str | None
    source_version_id: str | None
    uploaded_by: uuid.UUID
    created_at: dt.datetime
    updated_at: dt.datetime
