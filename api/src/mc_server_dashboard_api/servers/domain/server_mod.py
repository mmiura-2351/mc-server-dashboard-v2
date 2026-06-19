"""Entity and value objects for server↔mod assignment (issue #1262).

A :class:`ServerModAssignment` links a library :class:`~...servers.domain.mod.Mod`
to a server (the server's "mod set"). The assignment is many-to-many: a server may
select many mods, and a library mod may be assigned to many servers. ``enabled``
toggles whether the deployed jar is active (a disabled assignment renames the
deployed file to ``<filename>.disabled`` rather than deleting it).

The physical jar placement into the server's working set is the assignment use
cases' job (``servers.application.server_mods``); this entity only indexes the
link.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.mod import ModId
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


@dataclass(frozen=True)
class ServerModId:
    """The identity of a :class:`ServerModAssignment` (a UUID primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> ServerModId:
        """Generate a fresh, random assignment id."""

        return cls(uuid.uuid4())


@dataclass
class ServerModAssignment:
    """Row of the ``server_mods`` table (migration 0020).

    Links a server to a library mod (``UNIQUE(server_id, mod_id)``). ``enabled``
    controls whether the deployed jar is active. ``assigned_by`` is the acting
    user (a plain UUID, no FK so the row survives the user's deletion).
    """

    id: ServerModId
    server_id: ServerId
    mod_id: ModId
    enabled: bool
    assigned_by: uuid.UUID
    created_at: dt.datetime
    updated_at: dt.datetime
