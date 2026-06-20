"""Validate a server's installed plugin set (issue #1307, epic #650 phase B).

A pure function over (server loader + MC version, the server's installed
plugins) that surfaces a checklist of problems the operator should fix by hand.
**Display only** -- it never mutates the set; auto-resolution is item 6 (#1309).

Four finding kinds:

* ``missing_deps`` -- a required dependency of an installed plugin whose target
  ``mod_identifier`` is not present anywhere in the set. A dependency's id is
  present when it appears as some plugin's ``mod_identifier`` *or* in that
  plugin's ``provides`` list. This catches the canonical "Fabric API entirely
  absent" failure.
* ``version_unsatisfied`` -- the dependency's target id **is** present, but the
  present plugin's ``version_number`` does not satisfy the required
  ``version_range``. The range is evaluated by
  :func:`version_range.version_satisfies` in the server's loader dialect; an
  empty/unparseable range is treated as "any" and never flagged.
* ``conflicts`` -- a dependency entry explicitly marked as a break/conflict whose
  target id is present in the set. The manifest parser emits these entries: a
  declared ``breaks``/incompatible relation is stored as a dependency dict
  carrying a ``conflict`` flag.
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

from mc_server_dashboard_api.servers.application.version_range import version_satisfies
from mc_server_dashboard_api.servers.domain.plugin import ServerPlugin

# The version-range dialect used for each server loader. Forge/NeoForge ranges
# are Maven intervals; everything else uses semver predicates (see
# :mod:`version_range`). A server runs a single loader family, so the dialect is
# the same for every plugin on it.
_MAVEN_SERVER_TYPES = frozenset({"forge"})


@dataclass(frozen=True)
class MissingDependency:
    """A required dependency of ``mod_id`` that nothing in the set provides."""

    mod_id: str
    depends_on: str
    version_range: str


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
    version_unsatisfied: list[VersionUnsatisfied] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    mc_mismatch: list[McMismatch] = field(default_factory=list)


def validate_plugin_set(
    *, server_type: str, mc_version: str, plugins: list[ServerPlugin]
) -> PluginValidation:
    """Run the phase-B validation pass over a server's installed plugin set."""

    loader = "forge" if server_type in _MAVEN_SERVER_TYPES else "fabric"
    provided = _provided_versions(plugins)
    missing_deps, version_unsatisfied = _required_dep_findings(
        plugins, provided, loader
    )
    return PluginValidation(
        missing_deps=missing_deps,
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


def _conflicts(plugins: list[ServerPlugin], provided: set[str]) -> list[Conflict]:
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
    return findings


def _mc_mismatch(
    plugins: list[ServerPlugin], mc_version: str, loader: str
) -> list[McMismatch]:
    """Flag plugins that do not cover the server's MC version.

    Each declared ``mc_versions`` entry is matched against the server version in
    the loader's range dialect, so a Forge interval (``[1.20.4,1.21)``) and a
    Fabric exact/predicate (``1.20.4``, ``~1.20``) both work; a plain version
    list still matches by membership. A plugin declaring no versions is
    unconstrained and never flagged.
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
