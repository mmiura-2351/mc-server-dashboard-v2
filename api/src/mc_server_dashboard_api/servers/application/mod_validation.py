"""Validate a server's selected mod set (issue #1263, epic #1258 phase B).

A pure function over (server loader + MC version, the assigned library mods) that
surfaces a checklist of problems the operator should fix by hand. **Display only**
-- it never mutates the mod set; auto-resolution is phase C (#1268).

Five finding kinds:

* ``missing_deps`` -- a required dependency of an assigned mod whose target
  ``mod_identifier`` is not present anywhere in the set. A dependency's id is
  present when it appears as some assigned mod's ``mod_identifier`` *or* in that
  mod's ``provides`` list. This catches the canonical "Fabric API entirely
  absent" failure.
* ``version_unsatisfied`` -- the dependency's target id **is** present, but the
  present mod's ``version_number`` does not satisfy the required ``version_range``
  (phase C, #1293). The range is evaluated by
  :func:`version_range.version_satisfies` in the depending mod's loader dialect;
  an empty/unparseable range is treated as "any" and never flagged. This is the
  range-satisfaction upgrade over the phase-B presence-only check.
* ``conflicts`` -- a dependency entry explicitly marked as a break/conflict whose
  target id is present in the set. The manifest parser now emits these entries
  (#1288): a declared ``breaks``/incompatible relation is stored as a dependency
  dict carrying a ``conflict`` flag, so this list is populated from parsed data.
  The check reads that optional ``conflict`` flag on the dependency dict.
* ``loader_mismatch`` -- an assigned mod whose ``loader_type`` is incompatible
  with the server's loader (see :data:`_LOADER_COMPAT`). A phase-B *warning*, not
  a hard gate.
* ``mc_mismatch`` -- an assigned mod whose ``mc_versions`` list does not include
  the server's ``mc_version`` (simple membership; a mod that declares no versions
  is treated as unconstrained and never flagged).

Pure: no I/O, no DB, standard library only. The use case calls it and attaches
the result; the edge serialises it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.application.version_range import version_satisfies
from mc_server_dashboard_api.servers.domain.mod import Mod

# Which library mod loaders a server loader can run. The server loader is the
# ``server_type`` (vanilla/paper/fabric/forge/spigot); the mod loader is the
# library entry's ``loader_type`` (fabric/forge/neoforge/quilt/paper).
#
# * fabric  <-> fabric / quilt mods (Quilt is Fabric-compatible)
# * forge   <-> forge / neoforge mods (NeoForge is a Forge fork)
# * paper   <-> paper plugins
# * spigot  <-> paper plugins (Paper is a Spigot fork; Bukkit/Spigot plugins load)
# * vanilla <-> nothing (a vanilla server runs no mod loader)
#
# This map is the canonical loader-compatibility policy (issue #1286). The webui
# assign-dialog filter mirrors it as ``LOADER_COMPAT`` in
# ``webui/src/pages/ServerModsSection.tsx``; keep the two in sync.
_LOADER_COMPAT: dict[str, frozenset[str]] = {
    "fabric": frozenset({"fabric", "quilt"}),
    "forge": frozenset({"forge", "neoforge"}),
    "paper": frozenset({"paper"}),
    "spigot": frozenset({"paper"}),
    "vanilla": frozenset(),
}


@dataclass(frozen=True)
class MissingDependency:
    """A required dependency of ``mod_identifier`` that nothing in the set provides."""

    mod_id: str
    """The assigned mod (its ``mod_identifier``) that declares the dependency."""
    depends_on: str
    """The unsatisfied dependency's target ``mod_identifier``."""
    version_range: str
    """The required range, surfaced for the human (not range-checked in v1)."""


@dataclass(frozen=True)
class VersionUnsatisfied:
    """A required dependency that is present but at a version outside its range."""

    mod_id: str
    """The assigned mod (its ``mod_identifier``) that declares the dependency."""
    depends_on: str
    """The dependency's target ``mod_identifier``, which is present in the set."""
    version_range: str
    """The required range the present version fails to satisfy."""
    present_version: str
    """The version of the present mod that satisfies the dependency's id."""


@dataclass(frozen=True)
class Conflict:
    """An assigned mod that declares a break/conflict on another present mod."""

    mod_id: str
    """The assigned mod (its ``mod_identifier``) that declares the conflict."""
    conflicts_with: str
    """The conflicting target ``mod_identifier`` that is present in the set."""


@dataclass(frozen=True)
class LoaderMismatch:
    """An assigned mod whose loader the server cannot run."""

    mod_id: str
    """The assigned mod's ``mod_identifier``."""
    mod_loader: str
    """The mod's ``loader_type``."""
    server_loader: str
    """The server's ``server_type``."""


@dataclass(frozen=True)
class McMismatch:
    """An assigned mod that does not list the server's MC version."""

    mod_id: str
    """The assigned mod's ``mod_identifier``."""
    mod_mc_versions: list[str]
    """The MC versions the mod declares (empty == unconstrained, never flagged)."""
    server_mc_version: str
    """The server's ``mc_version``."""


@dataclass(frozen=True)
class ModValidation:
    """The full checklist for a server's mod set; all-empty == fully valid."""

    missing_deps: list[MissingDependency] = field(default_factory=list)
    version_unsatisfied: list[VersionUnsatisfied] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    loader_mismatch: list[LoaderMismatch] = field(default_factory=list)
    mc_mismatch: list[McMismatch] = field(default_factory=list)


def validate_mod_set(
    *, server_type: str, mc_version: str, mods: list[Mod]
) -> ModValidation:
    """Run the phase-B validation pass over a server's selected mod set."""

    provided = _provided_versions(mods)
    missing_deps, version_unsatisfied = _required_dep_findings(mods, provided)
    return ModValidation(
        missing_deps=missing_deps,
        version_unsatisfied=version_unsatisfied,
        conflicts=_conflicts(mods, set(provided)),
        loader_mismatch=_loader_mismatch(mods, server_type),
        mc_mismatch=_mc_mismatch(mods, mc_version),
    )


def _provided_versions(mods: list[Mod]) -> dict[str, str]:
    """Map every id the set satisfies to a providing mod's ``version_number``.

    An id is provided by a mod's own ``mod_identifier`` or by its ``provides``
    list; a provided id inherits the providing jar's version (the best version
    information available for it). When two mods provide the same id the last one
    wins -- presence is what matters and a multi-provider set is itself unusual.
    """

    provided: dict[str, str] = {}
    for mod in mods:
        provided[mod.mod_identifier] = mod.version_number
        for pid in mod.provides:
            provided[pid] = mod.version_number
    return provided


def _required_dep_findings(
    mods: list[Mod], provided: dict[str, str]
) -> tuple[list[MissingDependency], list[VersionUnsatisfied]]:
    """Split required-dependency problems into absent vs present-but-out-of-range."""

    missing: list[MissingDependency] = []
    unsatisfied: list[VersionUnsatisfied] = []
    for mod in mods:
        for dep in mod.dependencies:
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
                        mod_id=mod.mod_identifier,
                        depends_on=target,
                        version_range=version_range,
                    )
                )
                continue
            present_version = provided[target]
            if version_satisfies(present_version, version_range, mod.loader_type):
                continue
            unsatisfied.append(
                VersionUnsatisfied(
                    mod_id=mod.mod_identifier,
                    depends_on=target,
                    version_range=version_range,
                    present_version=present_version,
                )
            )
    return missing, unsatisfied


def _conflicts(mods: list[Mod], provided: set[str]) -> list[Conflict]:
    findings: list[Conflict] = []
    for mod in mods:
        for dep in mod.dependencies:
            if not dep.get("conflict"):
                continue
            target = dep.get("mod_identifier")
            if not isinstance(target, str) or target not in provided:
                continue
            findings.append(Conflict(mod_id=mod.mod_identifier, conflicts_with=target))
    return findings


def _loader_mismatch(mods: list[Mod], server_type: str) -> list[LoaderMismatch]:
    compatible = _LOADER_COMPAT.get(server_type, frozenset())
    return [
        LoaderMismatch(
            mod_id=mod.mod_identifier,
            mod_loader=mod.loader_type,
            server_loader=server_type,
        )
        for mod in mods
        if mod.loader_type not in compatible
    ]


def _mc_mismatch(mods: list[Mod], mc_version: str) -> list[McMismatch]:
    return [
        McMismatch(
            mod_id=mod.mod_identifier,
            mod_mc_versions=mod.mc_versions,
            server_mc_version=mc_version,
        )
        for mod in mods
        if mod.mc_versions and mc_version not in mod.mc_versions
    ]
