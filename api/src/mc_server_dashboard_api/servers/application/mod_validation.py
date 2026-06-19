"""Validate a server's selected mod set (issue #1263, epic #1258 phase B).

A pure function over (server loader + MC version, the assigned library mods) that
surfaces a checklist of problems the operator should fix by hand. **Display only**
-- it never mutates the mod set; auto-resolution is phase C (#1268).

Four finding kinds:

* ``missing_deps`` -- a required dependency of an assigned mod whose target
  ``mod_identifier`` is not satisfied anywhere in the set. v1 is a **presence
  check**: a dependency is satisfied when its id appears as some assigned mod's
  ``mod_identifier`` *or* in that mod's ``provides`` list. The required
  ``version_range`` is surfaced for the human but **not** range-checked (full
  version-range satisfaction is phase C, #1268). This catches the canonical
  "Fabric API entirely absent" failure without a constraint solver.
* ``conflicts`` -- a dependency entry explicitly marked as a break/conflict whose
  target id is present in the set. The current manifest parser does **not** emit
  break/conflict entries (it stores only ``required`` deps), so this list is
  empty for today's data; the check reads an optional ``conflict`` flag on the
  dependency dict so it works the moment the parser surfaces breaks, without
  inventing new parser output here.
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
    conflicts: list[Conflict] = field(default_factory=list)
    loader_mismatch: list[LoaderMismatch] = field(default_factory=list)
    mc_mismatch: list[McMismatch] = field(default_factory=list)


def validate_mod_set(
    *, server_type: str, mc_version: str, mods: list[Mod]
) -> ModValidation:
    """Run the phase-B validation pass over a server's selected mod set."""

    provided = _provided_identifiers(mods)
    return ModValidation(
        missing_deps=_missing_deps(mods, provided),
        conflicts=_conflicts(mods, provided),
        loader_mismatch=_loader_mismatch(mods, server_type),
        mc_mismatch=_mc_mismatch(mods, mc_version),
    )


def _provided_identifiers(mods: list[Mod]) -> set[str]:
    """Every id the set satisfies: each mod's id plus everything it ``provides``."""

    provided: set[str] = set()
    for mod in mods:
        provided.add(mod.mod_identifier)
        provided.update(mod.provides)
    return provided


def _missing_deps(mods: list[Mod], provided: set[str]) -> list[MissingDependency]:
    findings: list[MissingDependency] = []
    for mod in mods:
        for dep in mod.dependencies:
            if not dep.get("required"):
                continue
            target = dep.get("mod_identifier")
            if not isinstance(target, str) or target in provided:
                continue
            version_range = dep.get("version_range")
            findings.append(
                MissingDependency(
                    mod_id=mod.mod_identifier,
                    depends_on=target,
                    version_range=version_range
                    if isinstance(version_range, str)
                    else "",
                )
            )
    return findings


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
