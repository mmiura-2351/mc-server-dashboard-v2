"""Entity and value objects for server plugin/mod content management.

A :class:`ServerPlugin` is the metadata of a plugin or mod jar installed in a
server's content directory (``mods/`` for Fabric/Forge, ``plugins/`` for Paper).
The jar bytes live behind the :class:`FileStore` seam; this entity only indexes
them. The shape mirrors the ``server_plugin`` table (migration 0018).

Plugins live inside the servers context: they share the ``Server`` aggregate,
the ``(community_id, server_id)`` scope, the at-rest state policy, and the
FileStore seam, so a separate context would only re-import all of that across
a boundary.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from dataclasses import dataclass, field
from typing import Literal

from mc_server_dashboard_api.servers.domain.errors import (
    UnsupportedPluginServerTypeError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId, ServerType

# Where a mod/plugin is needed: ``server`` only, ``client`` only, or ``both``
# (issue #1308). ``both`` is the safe default -- a ``both`` jar is present
# everywhere -- so it is used whenever the side cannot be detected.
PluginSide = Literal["server", "client", "both"]


@dataclass(frozen=True)
class PluginId:
    """The identity of a :class:`ServerPlugin` (a UUID primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> PluginId:
        """Generate a fresh, random plugin id."""

        return cls(uuid.uuid4())


class LoaderType(enum.Enum):
    """The loader family the content targets (mod loader vs plugin API)."""

    MOD = "mod"
    PLUGIN = "plugin"


class PluginSource(enum.Enum):
    """How the plugin was obtained."""

    LOCAL = "local"
    MODRINTH = "modrinth"


@dataclass
class ServerPlugin:
    """Row of the ``server_plugin`` table (migration 0018).

    ``rel_path`` is the relative path within the working set (e.g.
    ``mods/fabric-api.jar``). ``source_project_id`` / ``source_version_id``
    carry Modrinth provenance when present; ``checksum_sha512`` is the SHA-512
    of the jar bytes at install time. ``sha256`` is the content address of the
    jar in the content-addressed cache (issue #1306): identical content shares
    one cached blob keyed by this hash. ``enabled`` tracks the
    ``.disabled``-suffix rename convention: a disabled plugin's ``rel_path``
    ends with ``.disabled``.

    ``mod_identifier`` / ``provides`` / ``dependencies`` / ``mc_versions`` carry
    the jar manifest metadata parsed at ingest (issue #1307): the uniform
    dependency source for both local uploads and Modrinth installs.
    ``mod_identifier`` is the manifest's declared id (``None`` when the jar
    carried no recognized manifest); ``provides`` are alias ids the jar also
    satisfies; ``dependencies`` use the shape ``[{"mod_identifier",
    "version_range", "required", "conflict"}]``; ``mc_versions`` are the declared
    compatible Minecraft versions. The phase-B validator reads these to surface
    missing required deps, conflicts, and MC-version mismatch.

    ``side`` (issue #1308) is where the content is needed -- ``server``,
    ``client``, or ``both`` -- auto-detected at ingest and manually overridable.
    It governs working-set presence: only a jar with side in {``server``,
    ``both``} deploys to the running server (see :func:`working_set_present`); a
    ``client``-only jar is tracked and cached but never placed in the working set.
    """

    id: PluginId
    server_id: ServerId
    rel_path: str
    filename: str
    display_name: str
    description: str | None
    loader_type: LoaderType
    source: PluginSource
    source_project_id: str | None
    source_version_id: str | None
    version_number: str | None
    checksum_sha512: str | None
    sha256: str | None
    size_bytes: int | None
    enabled: bool
    installed_by: uuid.UUID | None
    created_at: dt.datetime
    updated_at: dt.datetime
    mod_identifier: str | None = None
    provides: list[str] = field(default_factory=list)
    dependencies: list[dict[str, object]] = field(default_factory=list)
    mc_versions: list[str] = field(default_factory=list)
    side: PluginSide = "both"


def working_set_present(*, enabled: bool, side: PluginSide) -> bool:
    """Whether a plugin's jar belongs in the running server's working set.

    The observable deployment contract (issue #1308): the working set holds
    exactly the **enabled** jars whose side is server-relevant (``server`` or
    ``both``). A ``client``-only jar is tracked and cached but never deployed; a
    disabled jar is not running either (its working-set file is removed / suffixed
    ``.disabled``).
    """

    return enabled and side != "client"


def content_dir_for_server_type(server_type: ServerType) -> str:
    """Return the content directory name for ``server_type``.

    Fabric and Forge use ``mods/``; Paper uses ``plugins/``. Vanilla and Spigot
    do not support managed content and raise
    :class:`UnsupportedPluginServerTypeError`.
    """

    if server_type in (ServerType.FABRIC, ServerType.FORGE):
        return "mods"
    if server_type is ServerType.PAPER:
        return "plugins"
    raise UnsupportedPluginServerTypeError(server_type.value)


def loader_type_for_server_type(server_type: ServerType) -> LoaderType:
    """Return the :class:`LoaderType` for ``server_type``.

    Same mapping as :func:`content_dir_for_server_type`: Fabric/Forge are mods,
    Paper is plugins. Vanilla/Spigot raise.
    """

    if server_type in (ServerType.FABRIC, ServerType.FORGE):
        return LoaderType.MOD
    if server_type is ServerType.PAPER:
        return LoaderType.PLUGIN
    raise UnsupportedPluginServerTypeError(server_type.value)


_MODRINTH_LOADER_MAP: dict[ServerType, str] = {
    ServerType.FABRIC: "fabric",
    ServerType.FORGE: "forge",
    ServerType.PAPER: "paper",
}


def modrinth_loader_for_server_type(server_type: ServerType) -> str:
    """Return the Modrinth ``loader`` facet string for ``server_type``.

    Fabric -> ``"fabric"``, Forge -> ``"forge"``, Paper -> ``"paper"``.
    Vanilla/Spigot raise :class:`UnsupportedPluginServerTypeError`.
    """

    loader = _MODRINTH_LOADER_MAP.get(server_type)
    if loader is None:
        raise UnsupportedPluginServerTypeError(server_type.value)
    return loader
