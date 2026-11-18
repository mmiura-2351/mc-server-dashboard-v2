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
import unicodedata
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.value_objects import ServerId

# The longest name a pack may carry, in UTF-8 bytes: ``NAME_MAX``, the limit a
# filesystem puts on one name and past which the file API answers
# ``name_too_long`` (issue #3907).
MAX_FILENAME_BYTES = 255


def is_valid_pack_filename(filename: str) -> bool:
    """Whether ``filename`` can name an uploaded pack (issue #3907).

    The name is the last segment of the pack's object key and of its public
    download URL, and the GC logs the key. So it must be one segment, with no
    ``/`` or ``\\``. It must carry no control character (Unicode ``Cc``: C0, DEL
    and C1), which would split a log line or drive the terminal showing it, and no
    lone surrogate, which has no UTF-8 encoding at all. It must not be blank, and
    must fit :data:`MAX_FILENAME_BYTES`. Any other character is a legitimate name,
    Unicode spaces and joiners included.

    Only an upload asks: a row stored before the check keeps its name, and every
    read path serves it as before.
    """

    return (
        filename.strip() != ""
        and not any(
            c in "/\\" or unicodedata.category(c) in ("Cc", "Cs") for c in filename
        )
        and len(filename.encode("utf-8")) <= MAX_FILENAME_BYTES
    )


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
