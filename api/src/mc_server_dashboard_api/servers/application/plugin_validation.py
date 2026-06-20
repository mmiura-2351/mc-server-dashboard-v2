"""Validate a server's installed plugin set (issue #1307, epic #650 phase B).

A pure function over (server loader + MC version, the server's installed
plugins) that surfaces a checklist of problems the operator should fix by hand.
**Display only** -- it never mutates the set; auto-resolution is item 6 (#1309).

Five finding kinds:

* ``missing_deps`` -- a required dependency of an installed plugin whose target
  ``mod_identifier`` is not present anywhere in the set. A dependency's id is
  present when it appears as some plugin's ``mod_identifier`` *or* in that
  plugin's ``provides`` list. This catches the canonical "Fabric API entirely
  absent" failure.
* ``missing_catalog_deps`` -- a required **Modrinth catalog** dependency of a
  Modrinth-sourced plugin (issue #1321) whose ``project_id`` is not present
  anywhere in the set. Catalog deps live in a different namespace from manifest
  deps: they are keyed by ``project_id`` and satisfied iff some installed plugin
  has a matching ``source_project_id``. Many mods (e.g. Roughly Enough Items)
  declare deps only in their Modrinth metadata, not the jar manifest, so this
  surfaces what the manifest-driven ``missing_deps`` cannot. Captured at ingest.
* ``version_unsatisfied`` -- the dependency's target id **is** present, but the
  present plugin's ``version_number`` does not satisfy the required
  ``version_range``. The range is evaluated by
  :func:`version_range.version_satisfies` in the server's loader dialect; an
  empty/unparseable range is treated as "any" and never flagged.
* ``conflicts`` -- a dependency entry explicitly marked as a break/conflict whose
  target id is present in the set. The manifest parser emits these entries: a
  declared ``breaks``/incompatible relation is stored as a dependency dict
  carrying a ``conflict`` flag. A Modrinth catalog ``incompatible`` edge (issue
  #1318), keyed by ``project_id``, is also flagged when an installed plugin has
  that ``source_project_id``.
* ``mc_mismatch`` -- an installed plugin none of whose declared ``mc_versions``
  cover the server's ``mc_version``. Each entry is evaluated in the loader's
  range dialect (a Forge Maven interval, a Fabric predicate, or a plain version),
  so a single interval like ``[1.20.4,1.21)`` is matched correctly. A plugin that
  declares no versions is unconstrained and never flagged.

Per-server adaptation: there is no per-plugin loader-mismatch finding. In the
per-server model every plugin is installed for exactly this server, so they all
share the server's loader family -- a loader mismatch is structurally impossible.

Pure: no I/O, no DB, standard library only. The use case calls it and attaches
the result; the edge serialises it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.application.catalog_deps import (
    incompatible_catalog_deps,
    installed_project_ids,
    required_catalog_deps,
)
from mc_server_dashboard_api.servers.application.version_range import version_satisfies
from mc_server_dashboard_api.servers.domain.plugin import ServerPlugin

# The version-range dialect used for each server loader (see :mod:`version_range`).
# Forge/NeoForge ranges are Maven intervals; Paper/Spigot MC compat is a Bukkit
# ``api-version`` floor; everything else uses semver predicates. A server runs a
# single loader family, so the dialect is the same for every plugin on it.
_MAVEN_SERVER_TYPES = frozenset({"forge"})
_PAPER_SERVER_TYPES = frozenset({"paper", "spigot"})


def _loader_dialect(server_type: str) -> str:
    """The :mod:`version_range` dialect key for a server's loader family."""

    if server_type in _MAVEN_SERVER_TYPES:
        return "forge"
    if server_type in _PAPER_SERVER_TYPES:
        return "paper"
    return "fabric"


@dataclass(frozen=True)
class MissingDependency:
    """A required dependency of ``mod_id`` that nothing in the set provides."""

    mod_id: str
    depends_on: str
    version_range: str


@dataclass(frozen=True)
class MissingCatalogDependency:
    """A required Modrinth catalog dep of ``mod_id`` no installed project covers.

    Keyed by ``project_id`` (the Modrinth namespace, distinct from the manifest
    ``mod_identifier`` deps). ``slug`` / ``title`` are the dep project's
    human-readable label, captured at ingest so display needs no extra round-trip
    (either may be ``None`` if the catalog did not return it at install time).
    """

    mod_id: str
    project_id: str
    slug: str | None
    title: str | None


@dataclass(frozen=True)
class VersionUnsatisfied:
    """A required dependency that is present but at a version outside its range."""

    mod_id: str
    depends_on: str
    version_range: str
    present_version: str


@dataclass(frozen=True)
class Conflict:
    """An installed plugin that declares a break/conflict on another present one."""

    mod_id: str
    conflicts_with: str


@dataclass(frozen=True)
class McMismatch:
    """An installed plugin that does not list the server's MC version."""

    mod_id: str
    mod_mc_versions: list[str]
    server_mc_version: str


@dataclass(frozen=True)
class PluginValidation:
    """The full checklist for a server's plugin set; all-empty == fully valid."""

    missing_deps: list[MissingDependency] = field(default_factory=list)
    missing_catalog_deps: list[MissingCatalogDependency] = field(default_factory=list)
    version_unsatisfied: list[VersionUnsatisfied] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    mc_mismatch: list[McMismatch] = field(default_factory=list)


def validate_plugin_set(
    *, server_type: str, mc_version: str, plugins: list[ServerPlugin]
) -> PluginValidation:
    """Run the phase-B validation pass over a server's installed plugin set."""

    loader = _loader_dialect(server_type)
    provided = _provided_versions(plugins)
    missing_deps, version_unsatisfied = _required_dep_findings(
        plugins, provided, loader
    )
    return PluginValidation(
        missing_deps=missing_deps,
        missing_catalog_deps=_missing_catalog_deps(plugins),
        version_unsatisfied=version_unsatisfied,
        conflicts=_conflicts(plugins, set(provided)),
        mc_mismatch=_mc_mismatch(plugins, mc_version, loader),
    )


def _provided_versions(plugins: list[ServerPlugin]) -> dict[str, str]:
    """Map every id the set satisfies to a providing plugin's ``version_number``.

    An id is provided by a plugin's own ``mod_identifier`` or by its ``provides``
    list; a provided id inherits the providing jar's version. A plugin with no
    parsed ``mod_identifier`` (no recognized manifest) provides nothing.
    """

    provided: dict[str, str] = {}
    for plugin in plugins:
        if not plugin.mod_identifier:
            continue
        version = plugin.version_number or ""
        provided[plugin.mod_identifier] = version
        for pid in plugin.provides:
            provided[pid] = version
    return provided


def _required_dep_findings(
    plugins: list[ServerPlugin], provided: dict[str, str], loader: str
) -> tuple[list[MissingDependency], list[VersionUnsatisfied]]:
    """Split required-dependency problems into absent vs present-but-out-of-range."""

    missing: list[MissingDependency] = []
    unsatisfied: list[VersionUnsatisfied] = []
    for plugin in plugins:
        if not plugin.mod_identifier:
            continue
        for dep in plugin.dependencies:
            if not dep.get("required"):
                continue
            target = dep.get("mod_identifier")
            if not isinstance(target, str):
                continue
            raw_range = dep.get("version_range")
            version_range = raw_range if isinstance(raw_range, str) else ""
            if target not in provided:
                missing.append(
                    MissingDependency(
                        mod_id=plugin.mod_identifier,
                        depends_on=target,
                        version_range=version_range,
                    )
                )
                continue
            present_version = provided[target]
            if version_satisfies(present_version, version_range, loader):
                continue
            unsatisfied.append(
                VersionUnsatisfied(
                    mod_id=plugin.mod_identifier,
                    depends_on=target,
                    version_range=version_range,
                    present_version=present_version,
                )
            )
    return missing, unsatisfied


def _missing_catalog_deps(
    plugins: list[ServerPlugin],
) -> list[MissingCatalogDependency]:
    """Flag each required Modrinth catalog dep no installed project covers.

    Evaluated only for Modrinth-sourced plugins (a local upload has no catalog
    deps). A dep with ``project_id = X`` is satisfied iff some installed plugin
    has ``source_project_id == X``; otherwise it is a missing-dependency finding
    carrying the dep project's slug/title for display.
    """

    present = installed_project_ids(plugins)
    findings: list[MissingCatalogDependency] = []
    for plugin in plugins:
        if not plugin.mod_identifier:
            continue
        for dep in required_catalog_deps(plugin):
            if dep.project_id in present:
                continue
            findings.append(
                MissingCatalogDependency(
                    mod_id=plugin.mod_identifier,
                    project_id=dep.project_id,
                    slug=dep.slug,
                    title=dep.title,
                )
            )
    return findings


def _conflicts(plugins: list[ServerPlugin], provided: set[str]) -> list[Conflict]:
    """Flag manifest ``conflict`` edges and Modrinth catalog ``incompatible`` edges.

    A manifest ``breaks``/``conflicts`` edge is flagged when its target id is
    present (the ``mod_identifier`` namespace). A Modrinth catalog ``incompatible``
    edge (issue #1318) is keyed by ``project_id`` and flagged when an installed
    plugin has that ``source_project_id``; the finding reports that plugin's
    ``mod_identifier`` so the checklist stays in the same namespace as the rest.
    """

    by_project_id = {
        plugin.source_project_id: plugin.mod_identifier
        for plugin in plugins
        if plugin.source_project_id is not None and plugin.mod_identifier
    }
    findings: list[Conflict] = []
    for plugin in plugins:
        if not plugin.mod_identifier:
            continue
        for dep in plugin.dependencies:
            if not dep.get("conflict"):
                continue
            target = dep.get("mod_identifier")
            if not isinstance(target, str) or target not in provided:
                continue
            findings.append(
                Conflict(mod_id=plugin.mod_identifier, conflicts_with=target)
            )
        for cdep in incompatible_catalog_deps(plugin):
            target_id = by_project_id.get(cdep.project_id)
            if target_id is None:
                continue
            findings.append(
                Conflict(mod_id=plugin.mod_identifier, conflicts_with=target_id)
            )
    return findings


def _mc_mismatch(
    plugins: list[ServerPlugin], mc_version: str, loader: str
) -> list[McMismatch]:
    """Flag plugins that do not cover the server's MC version.

    Each declared ``mc_versions`` entry is matched against the server version in
    the loader's range dialect, so a Forge interval (``[1.20.4,1.21)``), a Fabric
    exact/predicate (``1.20.4``, ``~1.20``), and a Bukkit ``api-version`` floor
    (``1.21`` covers any ``1.21.x`` or newer) all work; a plain version list still
    matches by membership. A plugin declaring no versions is unconstrained and
    never flagged.
    """

    return [
        McMismatch(
            mod_id=plugin.mod_identifier,
            mod_mc_versions=plugin.mc_versions,
            server_mc_version=mc_version,
        )
        for plugin in plugins
        if plugin.mod_identifier
        and plugin.mc_versions
        and not any(
            version_satisfies(mc_version, entry, loader) for entry in plugin.mc_versions
        )
    ]
