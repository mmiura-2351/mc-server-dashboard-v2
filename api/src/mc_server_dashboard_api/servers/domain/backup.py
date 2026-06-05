"""Entity and value objects for backup metadata (DATABASE.md Section 8).

A :class:`Backup` is the *metadata* of a retained snapshot for a server
(FR-BAK-1): it points at the archive bytes that live behind the ``Storage`` Port
(STORAGE.md Section 3.3) by an opaque ``storage_ref`` and records who/why/when.
The bytes are not here — this row only indexes them. The shape mirrors
DATABASE.md Section 8's ``backup`` table.

Backups live inside the servers context: they share the ``Server`` aggregate, the
``(community_id, server_id)`` scope, the control-plane seam (save-all + on-demand
snapshot for the running path, Section 6.9) and the at-rest state policy, so a
separate context would only re-import all of that across a boundary.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.value_objects import ServerId


@dataclass(frozen=True)
class BackupId:
    """The identity of a :class:`Backup` (a UUID primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> BackupId:
        """Generate a fresh, random backup id."""

        return cls(uuid.uuid4())


class BackupSource(enum.Enum):
    """How a backup was produced (DATABASE.md Section 8 CHECK enum).

    ``MANUAL`` is an operator-requested backup (``backup:create``); ``SCHEDULED``
    is one produced by the per-server schedule (FR-BAK-3) — the listing of
    scheduled rows by ``created_at`` *is* the execution history. ``EVENT`` is
    reserved for event-driven backups (the doc's third value). ``UPLOADED`` is a
    backup brought in from an off-host archive via the upload endpoint (issue
    #281); M1 produces manual, scheduled, and uploaded.
    """

    MANUAL = "manual"
    SCHEDULED = "scheduled"
    EVENT = "event"
    UPLOADED = "uploaded"


@dataclass
class Backup:
    """Row of the ``backup`` table (DATABASE.md Section 8).

    ``storage_ref`` is the opaque locator of the archive in ``Storage`` (the
    ``BackupKey`` value). ``size_bytes`` is the recorded archive size when cheap
    to obtain, else ``None``. ``created_by`` is the acting member (nullable so the
    row survives the user's deletion and so a scheduled backup, which has no
    actor, records ``None``).

    There is **no** ``community_id`` column (DATABASE.md Section 8 lists none): a
    backup is owned by its ``server`` (FK cascade), and community scoping is
    enforced by the use case loading the server first (which is community-checked)
    and matching ``backup.server_id``.
    """

    id: BackupId
    server_id: ServerId
    storage_ref: str
    size_bytes: int | None
    source: BackupSource
    created_by: uuid.UUID | None
    created_at: dt.datetime


@dataclass(frozen=True)
class BackupStatistics:
    """Aggregate backup usage for a scope (one server, or the whole platform).

    ``count`` is the number of backups; ``total_bytes`` sums the *known* sizes
    (rows whose ``size_bytes`` is recorded); ``unknown_size_count`` is how many
    rows carry a NULL ``size_bytes`` (legacy rows created before the size was
    recorded, issue #281) and are therefore excluded from ``total_bytes`` — an
    honest "we don't know these" rather than a wrong total. ``newest`` / ``oldest``
    are the extreme ``created_at`` timestamps, or ``None`` when there are no
    backups.
    """

    count: int
    total_bytes: int
    unknown_size_count: int
    newest: dt.datetime | None
    oldest: dt.datetime | None
