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
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

from mc_server_dashboard_api.servers.domain.errors import (
    UnsupportedPluginServerTypeError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId, ServerType


def sanitize_plugin_filename(filename: str) -> str:
    """Reduce ``filename`` to a safe basename, stripping path components.

    Prevents zip-slip (issue #1400): a filename like ``a\\..\\..\\evil.jar``
    is reduced to ``evil.jar``. Raises :class:`ValueError` for filenames that
    are empty or ``..`` after sanitization.
    """

    safe = PurePosixPath(filename.replace("\\", "/")).name
    if not safe or safe == "..":
        raise ValueError(f"unsafe plugin filename: {filename!r}")
    return safe


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
    # GeyserMC's own download API (issue #1905): the only publisher of the
    # Floodgate-Spigot build, which Modrinth does not carry for Paper.
    GEYSER = "geyser"


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

    ``catalog_dependencies`` (issue #1321) are the **required** Modrinth catalog
    dependencies of a Modrinth-sourced plugin, captured at ingest from the
    selected version's ``dependencies``. Keyed by ``project_id`` (a different
    namespace from the manifest ``mod_identifier`` deps), they use the shape
    ``[{"project_id", "required", "slug", "title"}]`` -- the ``slug`` / ``title``
    carried so a human label needs no extra Modrinth round-trip. Many mods (e.g.
    Roughly Enough Items) declare deps only in Modrinth metadata, not the jar
    manifest, so validation/resolution also evaluate these (by ``project_id``)
    for Modrinth plugins. A local upload leaves this empty.
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
    catalog_dependencies: list[dict[str, object]] = field(default_factory=list)


# Geyser detection (issue #1541): installing Geyser as a normal plugin IS the
# Bedrock enablement switch, so ingest recognizes it by the identity the jar
# declares. The manifest name parsed at ingest (``plugin.yml`` ``name`` for
# Paper) is the primary signal, compared case-insensitively. Paper-only in v1;
# add the Fabric/Forge Geyser mod ids here when those loaders gain Bedrock
# support (epic #1540).
_GEYSER_MOD_IDENTIFIERS = frozenset({"geyser-spigot"})
# Secondary signal for catalog installs whose jar carried no readable manifest:
# the Modrinth Geyser project (https://modrinth.com/plugin/geyser). Installs may
# reference the project by its immutable id or its slug, and the plugin row
# stores whichever was used.
_GEYSER_MODRINTH_PROJECT_IDS = frozenset({"wKkoqHrH", "geyser"})


def is_geyser_plugin(plugin: ServerPlugin) -> bool:
    """Whether ``plugin`` is the Geyser Bedrock translator (issue #1541).

    Geyser presence drives the server's ``bedrock_port`` lifecycle: detected on
    install (allocate) and on uninstall (release). Floodgate is the expected
    companion but is NOT the detection key -- the network path hangs off Geyser,
    which owns the UDP listener.
    """

    identifier = (plugin.mod_identifier or "").lower()
    if identifier in _GEYSER_MOD_IDENTIFIERS:
        return True
    return plugin.source_project_id in _GEYSER_MODRINTH_PROJECT_IDS


def has_enabled_geyser(plugins: Iterable[ServerPlugin]) -> bool:
    """Whether ``plugins`` contains at least one *enabled* Geyser copy.

    The single predicate behind two independent "is this server actually
    Bedrock-reachable" checks that must not drift apart (issue #1555): the
    Bedrock tunnel dispatch skip (``ServersServerStateSink._sync_bedrock_tunnel``,
    issue #1544) and the ``ServerResponse`` ``bedrock_address``/``bedrock_port``
    surfacing gate. A disabled Geyser is not listening on its RakNet port, so
    neither the tunnel nor the response should treat the server as joinable.
    """

    return any(p.enabled and is_geyser_plugin(p) for p in plugins)


def working_set_present(*, enabled: bool, side: PluginSide) -> bool:
    """Whether a plugin's jar belongs in the running server's working set.

    The observable deployment contract (issue #1308): the working set holds
    exactly the **enabled** jars whose side is server-relevant (``server`` or
    ``both``). A ``client``-only jar is tracked and cached but never deployed; a
    disabled jar is not running either (its working-set file is removed / suffixed
    ``.disabled``).
    """

    return enabled and side != "client"


def working_set_path(*, clean_path: str, enabled: bool, side: PluginSide) -> str | None:
    """The working-set path a plugin's jar should occupy (issue #1308).

    The desired on-disk state derived from ``(enabled, side)``, given the
    suffix-free ``clean_path`` (e.g. ``mods/<name>.jar``):

    * ``side == "client"`` -> no working-set file (ever); ``None``.
    * server/both + enabled  -> the clean path.
    * server/both + disabled -> the ``.disabled`` path.

    Use cases reconcile the on-disk file to this single source of truth so the
    recorded ``rel_path`` and the actual file never drift (the ``.disabled``
    state-machine invariant).
    """

    if not working_set_present(enabled=True, side=side):
        return None
    return clean_path if enabled else f"{clean_path}.disabled"


def content_dir_for_server_type(server_type: ServerType) -> str:
    """Return the content directory name for ``server_type``.

    Fabric and Forge use ``mods/``; Paper uses ``plugins/``. Vanilla does not
    support managed content and raises
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
    Paper is plugins. Vanilla raises.
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
    Vanilla raises :class:`UnsupportedPluginServerTypeError`.
    """

    loader = _MODRINTH_LOADER_MAP.get(server_type)
    if loader is None:
        raise UnsupportedPluginServerTypeError(server_type.value)
    return loader
