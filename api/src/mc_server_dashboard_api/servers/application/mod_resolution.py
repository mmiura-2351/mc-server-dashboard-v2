"""Auto-resolve a server's missing required dependencies from the library (#1294).

Phase C / C2 of epic #1258: take the *direct* required dependencies of a server's
currently-assigned mod set and, for each one the set does not already satisfy,
look for a library mod that can satisfy it. The result is a **plan** — a
classification of every required dependency — that the edge can either show
(``GET .../mods/resolve``) or apply (``POST .../mods/resolve``, which assigns the
``resolvable_from_library`` picks via :class:`AssignMods`).

Classification of each direct required dependency:

* ``already_satisfied`` — the current set already provides the id at a version in
  range (the dep is not a finding). Re-running after apply yields this, so apply
  is idempotent.
* ``resolvable_from_library`` — a library mod (a) provides the id (via its own
  ``mod_identifier`` or ``provides``), (b) satisfies the dep's ``version_range``
  in the *depending* mod's loader dialect (C1 :func:`version_satisfies`), and (c)
  is loader/MC-compatible with the server. The chosen mod is the best candidate
  (highest satisfying ``version_number``; ties broken by mod id for determinism).
* ``needs_import`` — the id exists in the library but no candidate is both
  range- and server-compatible (e.g. only an out-of-range version is present, or
  every provider mismatches the loader/MC). C3 (#1295) would import a fit from
  Modrinth; here it is just classified.
* ``unresolvable`` — nothing in the library provides the id at all.

Scope (per #1294): only the DIRECT required deps of the currently-assigned set.
Transitive closure (deps-of-deps) is C4 (#1296); Modrinth auto-import is C3.

Pure: :func:`resolve_dependencies` does no I/O. The use cases load the data and,
for apply, delegate the mutation to :class:`AssignMods`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Literal

from mc_server_dashboard_api.servers.application.mod_validation import (
    _LOADER_COMPAT,
    ModValidation,
    validate_mod_set,
)
from mc_server_dashboard_api.servers.application.server_mods import (
    AssignMods,
    UnassignMod,
)
from mc_server_dashboard_api.servers.application.version_range import (
    compare_versions,
    version_satisfies,
)
from mc_server_dashboard_api.servers.domain.errors import ServerNotFoundError
from mc_server_dashboard_api.servers.domain.mod import Mod, ModId
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId

ResolutionStatus = Literal[
    "already_satisfied",
    "resolvable_from_library",
    "needs_import",
    "unresolvable",
]


@dataclass(frozen=True)
class ResolutionEntry:
    """One direct required dependency and how it can be resolved."""

    dep_identifier: str
    """The required dependency's target ``mod_identifier``."""
    required_range: str
    """The required ``version_range`` (empty == any)."""
    status: ResolutionStatus
    """How the dependency classifies against the current set and the library."""
    mod: Mod | None = None
    """The chosen library mod for ``resolvable_from_library``; ``None`` otherwise."""
    replaces: list[Mod] = field(default_factory=list)
    """Assigned mods this ``resolvable_from_library`` pick replaces.

    Non-empty only when the dep is present on the server at an out-of-range
    version (a ``version_unsatisfied`` finding): apply unassigns these stale
    same-``mod_identifier`` assignments before assigning ``mod``, so exactly one
    in-range version of the id remains (#1294). Empty for an absent dep — that
    is a plain add."""


@dataclass(frozen=True)
class ResolutionPlan:
    """A server's dependency-resolution plan plus its validation findings."""

    entries: list[ResolutionEntry] = field(default_factory=list)
    validation: ModValidation = field(default_factory=ModValidation)


@dataclass(frozen=True)
class _RequiredDep:
    """A direct required dependency, keyed for dedup across the assigned set."""

    identifier: str
    version_range: str
    loader: str


def _required_deps(assigned: list[Mod]) -> list[_RequiredDep]:
    """The distinct direct required deps of the assigned set, in first-seen order.

    Each dep carries the *depending* mod's loader so the range is evaluated in the
    right dialect (C1). Two assigned mods that declare the same ``(id, range,
    loader)`` collapse to one entry.
    """

    seen: set[tuple[str, str, str]] = set()
    deps: list[_RequiredDep] = []
    for mod in assigned:
        for dep in mod.dependencies:
            if not dep.get("required") or dep.get("conflict"):
                continue
            target = dep.get("mod_identifier")
            if not isinstance(target, str):
                continue
            raw_range = dep.get("version_range")
            version_range = raw_range if isinstance(raw_range, str) else ""
            key = (target, version_range, mod.loader_type)
            if key in seen:
                continue
            seen.add(key)
            deps.append(_RequiredDep(target, version_range, mod.loader_type))
    return deps


def _provided_versions(mods: list[Mod]) -> dict[str, str]:
    """Map every id the set satisfies to a providing mod's ``version_number``."""

    provided: dict[str, str] = {}
    for mod in mods:
        provided[mod.mod_identifier] = mod.version_number
        for pid in mod.provides:
            provided[pid] = mod.version_number
    return provided


def _provides(mod: Mod, identifier: str) -> bool:
    """Whether ``mod`` satisfies ``identifier`` (own id or a ``provides`` entry)."""

    return mod.mod_identifier == identifier or identifier in mod.provides


def _stale_providers(assigned: list[Mod], identifier: str, chosen: Mod) -> list[Mod]:
    """Assigned mods whose own id is ``identifier`` that a replacement supersedes.

    Only own-``mod_identifier`` matches are stale (a ``provides`` alias is a
    different host mod the replacement should not touch); the chosen library mod
    is excluded so a re-plan after apply yields no spurious replacement.
    """

    return [
        mod
        for mod in assigned
        if mod.mod_identifier == identifier and mod.id != chosen.id
    ]


def _server_compatible(mod: Mod, *, server_type: str, mc_version: str) -> bool:
    """Whether the server can run ``mod`` (loader compatible, MC version listed).

    Mirrors the validator's loader/MC checks: the loader must be in the server's
    compatible set, and the mod must list the server's MC version (an empty
    ``mc_versions`` is unconstrained).
    """

    if mod.loader_type not in _LOADER_COMPAT.get(server_type, frozenset()):
        return False
    return not mod.mc_versions or mc_version in mod.mc_versions


def _best_candidate(
    dep: _RequiredDep,
    *,
    server_type: str,
    mc_version: str,
    library: list[Mod],
) -> Mod | None:
    """The best library mod for ``dep``, or ``None`` if none fits the server/range.

    A candidate provides the id, satisfies the range in ``dep``'s loader dialect,
    and is loader/MC-compatible with the server. The best is the highest
    satisfying ``version_number``; ties break on the mod id so the choice is
    deterministic.
    """

    candidates = [
        mod
        for mod in library
        if _provides(mod, dep.identifier)
        and _server_compatible(mod, server_type=server_type, mc_version=mc_version)
        and version_satisfies(mod.version_number, dep.version_range, dep.loader)
    ]
    if not candidates:
        return None
    # Highest satisfying version wins; the mod id breaks ties for determinism.
    version_key = cmp_to_key(compare_versions)
    return max(
        candidates,
        key=lambda m: (version_key(m.version_number), str(m.id.value)),
    )


def resolve_dependencies(
    *,
    server_type: str,
    mc_version: str,
    assigned: list[Mod],
    library: list[Mod],
) -> ResolutionPlan:
    """Classify each direct required dep of ``assigned`` against ``library``.

    Pure: the caller supplies the assigned set, the library, and the server's
    loader/MC. The validation block is the same :func:`validate_mod_set` the edge
    already shows, recomputed here so the plan echoes the current findings.
    """

    provided = _provided_versions(assigned)
    entries: list[ResolutionEntry] = []
    for dep in _required_deps(assigned):
        present = provided.get(dep.identifier)
        if present is not None and version_satisfies(
            present, dep.version_range, dep.loader
        ):
            entries.append(
                ResolutionEntry(dep.identifier, dep.version_range, "already_satisfied")
            )
            continue
        candidate = _best_candidate(
            dep, server_type=server_type, mc_version=mc_version, library=library
        )
        if candidate is not None:
            # If the id is already assigned but out of range (a
            # ``version_unsatisfied`` finding, not an absent dep), the pick is a
            # replacement: apply unassigns the stale assignment(s) so exactly one
            # in-range version of the id remains. Only own-id providers are stale;
            # a ``provides`` alias is left alone (replacing it would drop the host
            # mod). An absent dep has no ``replaces`` — it is a plain add.
            replaces = (
                _stale_providers(assigned, dep.identifier, candidate)
                if present is not None
                else []
            )
            entries.append(
                ResolutionEntry(
                    dep.identifier,
                    dep.version_range,
                    "resolvable_from_library",
                    mod=candidate,
                    replaces=replaces,
                )
            )
        elif any(_provides(mod, dep.identifier) for mod in library):
            entries.append(
                ResolutionEntry(dep.identifier, dep.version_range, "needs_import")
            )
        else:
            entries.append(
                ResolutionEntry(dep.identifier, dep.version_range, "unresolvable")
            )

    validation = validate_mod_set(
        server_type=server_type, mc_version=mc_version, mods=assigned
    )
    return ResolutionPlan(entries=entries, validation=validation)


async def _load_resolution_inputs(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> tuple[str, str, list[Mod], list[Mod]]:
    """Load (server_type, mc_version, assigned mods, library) for resolution."""

    async with uow:
        server = await uow.servers.get_by_id(server_id)
        if server is None or server.community_id != community_id:
            raise ServerNotFoundError(str(server_id.value))

        assigned: list[Mod] = []
        for assignment in await uow.mods.list_assignments_for_server(server_id):
            mod = await uow.mods.get_by_id(assignment.mod_id)
            if mod is not None:
                assigned.append(mod)

        library = await uow.mods.list_all()
    return server.server_type.value, server.mc_version, assigned, library


@dataclass(frozen=True)
class ResolveServerMods:
    """Plan a server's dependency resolution against the library (read-only)."""

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> ResolutionPlan:
        server_type, mc_version, assigned, library = await _load_resolution_inputs(
            self.uow, community_id, server_id
        )
        return resolve_dependencies(
            server_type=server_type,
            mc_version=mc_version,
            assigned=assigned,
            library=library,
        )


@dataclass(frozen=True)
class ApplyServerModResolution:
    """Assign a server's ``resolvable_from_library`` deps, then re-plan (#1294).

    Plans against the current library, assigns every ``resolvable_from_library``
    pick through :class:`AssignMods` (which holds the lifecycle lock and is
    at-rest gated — a running server raises ``ServerFilesUnsettledError``), and
    returns the re-planned result. Idempotent: with nothing newly resolvable the
    assign list is empty and the returned plan shows the picks as
    ``already_satisfied``. Does NOT import from Modrinth (that is C3).

    A pick that carries ``replaces`` (the id is present but out of range — a
    ``version_unsatisfied`` finding) is a swap, not an add: the stale
    same-``mod_identifier`` assignment(s) are unassigned via :class:`UnassignMod`
    before the in-range library mod is assigned, so exactly one in-range version
    of the id remains and the re-plan converges to ``already_satisfied``.
    """

    uow: UnitOfWork
    assign_mods: AssignMods
    unassign_mod: UnassignMod

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        applied_by: uuid.UUID,
    ) -> tuple[ResolutionPlan, list[ModId]]:
        server_type, mc_version, assigned, library = await _load_resolution_inputs(
            self.uow, community_id, server_id
        )
        plan = resolve_dependencies(
            server_type=server_type,
            mc_version=mc_version,
            assigned=assigned,
            library=library,
        )

        # De-dup the picks: two deps can resolve to the same library mod. Collect
        # the stale assignments each replacement supersedes so they are unassigned
        # before the new version is added (never the chosen mod itself).
        picked: dict[ModId, None] = {}
        replaced: dict[ModId, None] = {}
        for entry in plan.entries:
            if entry.status == "resolvable_from_library" and entry.mod is not None:
                picked[entry.mod.id] = None
                for stale in entry.replaces:
                    replaced[stale.id] = None
        for chosen_id in picked:
            replaced.pop(chosen_id, None)
        applied = list(picked)

        if replaced:
            for stale_id in replaced:
                await self.unassign_mod(
                    community_id=community_id,
                    server_id=server_id,
                    mod_id=stale_id,
                )

        if applied:
            await self.assign_mods(
                community_id=community_id,
                server_id=server_id,
                mod_ids=applied,
                assigned_by=applied_by,
            )

        new_plan = await ResolveServerMods(self.uow)(
            community_id=community_id, server_id=server_id
        )
        return new_plan, applied
