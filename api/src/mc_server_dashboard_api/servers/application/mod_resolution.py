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
* ``needs_import`` — the id is missing from (or unsatisfiable by) the library, so
  it must be imported from Modrinth (#1295). The pure classifier marks it
  ``needs_import`` whenever no library candidate fits; the catalog enrichment then
  resolves a concrete ``will_import`` (project@version) for it, or — if Modrinth
  has nothing compatible — downgrades it to ``unresolvable``.
* ``unresolvable`` — neither the library nor Modrinth can satisfy the id.

C3 (#1295) folds Modrinth auto-import into the plan/apply (Modrinth-only;
CurseForge is deferred, #1269):

* :func:`resolve_imports_from_catalog` enriches each ``needs_import`` entry with a
  concrete ``will_import`` candidate. It locates the dependency's Modrinth project
  (a ``project_id`` carried on the dep edge if present, else a search by the dep's
  ``mod_identifier``) and selects the newest version compatible with the server's
  MC version, the server loader, and the required range (C1). Read-only: it only
  queries the catalog, never imports.
* ``GET .../mods/resolve`` returns the enriched plan (the ``will_import`` is a
  preview; nothing is downloaded).
* ``POST .../mods/resolve`` imports each ``will_import`` version into the library
  (reusing :class:`ImportMod` — sha256-dedup, so a jar already present is reused),
  assigns it via :class:`AssignMods`, then re-plans. A Modrinth lookup/import
  failure for one dep is isolated (recorded on that entry, the rest still apply).

C4 (#1296) extends resolution from the direct deps to the **full transitive
closure** (:func:`resolve_closure`):

* The walk is breadth-first from the assigned set. A resolved dep is added to the
  closure and its OWN required deps are then walked at the next depth — a library
  pick expands through the chosen jar's manifest ``dependencies``, a Modrinth
  ``will_import`` through the selected version's catalog dependency edges — so the
  plan covers transitively-required mods, not just first-level ones.
* It is **cycle-safe and bounded**: each dep id is classified at most once
  (a cycle A→B→A terminates), and the walk stops expanding past
  :data:`MAX_RESOLUTION_DEPTH`; a dep past the bound is reported ``depth_exceeded``
  rather than recursed. Each entry carries its ``depth`` and ``required_by`` so the
  transitive chain is visible in the plan.
* **Conflict detection spans the whole closure**: a resolved dep whose mod would
  break — or be broken by — a mod present or being added is marked ``blocked``,
  and the subtree that exists *only* because a blocked mod required it is pruned
  (also ``blocked``), so a transitive conflict blocks its whole orphaned subtree.
  Blocking and pruning iterate to a fixpoint — pruning a dep can change what
  conflicts another mod. A blocked entry is reported (so the user decides) but
  apply never auto-adds it.

Scope (per #1296): presence + per-edge range satisfaction across the closure; no
full SAT / optimal cross-graph version selection.

Pure: :func:`resolve_dependencies` does no I/O.
:func:`resolve_imports_from_catalog` and :func:`resolve_closure` do read-only
catalog I/O. The use cases load the data and, for apply, delegate the mutation to
:class:`ImportMod` / :class:`AssignMods`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Literal

from mc_server_dashboard_api.servers.application.mod_validation import (
    _LOADER_COMPAT,
    ModValidation,
    validate_mod_set,
)
from mc_server_dashboard_api.servers.application.mods import ImportMod
from mc_server_dashboard_api.servers.application.server_mods import (
    AssignMods,
    UnassignMod,
)
from mc_server_dashboard_api.servers.application.version_range import (
    compare_versions,
    version_satisfies,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogError,
    CatalogProject,
    CatalogProvider,
    CatalogVersion,
)
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidModJarError,
    ModIntegrityError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.mod import Mod, ModId
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId

_LOGGER = logging.getLogger(__name__)

ResolutionStatus = Literal[
    "already_satisfied",
    "resolvable_from_library",
    "needs_import",
    "unresolvable",
    "depth_exceeded",
]

#: How deep the transitive dependency walk recurses before it stops expanding
#: the frontier (#1296). A deeper-than-this dep is reported ``depth_exceeded``
#: rather than resolved, so a pathological graph can never loop or blow the stack.
MAX_RESOLUTION_DEPTH = 10


@dataclass(frozen=True)
class WillImport:
    """The concrete Modrinth project@version a ``needs_import`` dep resolves to.

    Carried on a ``needs_import`` :class:`ResolutionEntry` by the catalog
    enrichment (#1295). ``project_id`` / ``version_id`` drive the import on apply;
    ``slug`` / ``version_number`` are for the human-readable preview.
    """

    project_id: str
    version_id: str
    slug: str
    version_number: str


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
    will_import: WillImport | None = None
    """The Modrinth project@version a ``needs_import`` dep resolves to (#1295).

    Set by :func:`resolve_imports_from_catalog` for a ``needs_import`` entry when
    Modrinth has a compatible version; ``None`` otherwise. On apply this version is
    imported into the library and assigned."""
    depth: int = 0
    """How far from the assigned set this dep sits in the transitive walk (#1296).

    ``0`` is a direct required dep of an assigned mod; ``1`` is a dep of a mod
    resolved at depth 0; and so on. The walk stops expanding past
    :data:`MAX_RESOLUTION_DEPTH`."""
    required_by: str | None = None
    """The id whose resolution surfaced this dep, or ``None`` for a depth-0 dep.

    For a transitive dep it is the resolving mod's ``mod_identifier`` (or the
    depended-on Modrinth slug) that pulled this one in, so the chain is visible."""
    blocked: bool = False
    """Whether auto-adding this resolved dep would introduce a conflict (#1296).

    A resolved entry (``resolvable_from_library`` / ``needs_import``) is blocked
    when the mod it would add breaks — or is broken by — a mod already present or
    being added elsewhere in the closure. Apply skips a blocked entry: the user
    must resolve the conflict by hand."""


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


# ---------------------------------------------------------------------------
# Modrinth catalog enrichment (#1295)
# ---------------------------------------------------------------------------


def _select_import_version(
    project: CatalogProject,
    *,
    server_type: str,
    mc_version: str,
    version_range: str,
    range_loader: str,
) -> CatalogVersion | None:
    """The newest project version compatible with the server and the range.

    A version qualifies when it (a) lists the server's MC version in
    ``game_versions``, (b) declares a loader the server can run (the same
    loader-compat map the library resolver uses), and (c) satisfies the required
    ``version_range`` in the depending mod's loader dialect (C1). The newest
    ``version_number`` wins; the ``version_id`` breaks ties for determinism.
    ``None`` when no version qualifies.
    """

    compatible_loaders = _LOADER_COMPAT.get(server_type, frozenset())
    candidates = [
        version
        for version in project.versions
        if mc_version in version.game_versions
        and compatible_loaders.intersection(version.loaders)
        and version_satisfies(version.version_number, version_range, range_loader)
    ]
    if not candidates:
        return None
    version_key = cmp_to_key(compare_versions)
    return max(
        candidates,
        key=lambda v: (version_key(v.version_number), v.version_id),
    )


def _dep_project_id(assigned: list[Mod], dep_identifier: str) -> str | None:
    """A Modrinth ``project_id`` captured on the dep edge of an assigned mod.

    The preferred project lookup: when the depending mod was Modrinth-sourced its
    captured catalog ``dependencies`` may carry the depended-on project's id. Read
    a ``project_id`` key off the matching dep dict if present; ``None`` falls the
    resolver back to a search by ``dep_identifier``.
    """

    for mod in assigned:
        for dep in mod.dependencies:
            if dep.get("mod_identifier") != dep_identifier:
                continue
            project_id = dep.get("project_id")
            if isinstance(project_id, str) and project_id:
                return project_id
    return None


async def _locate_project(
    catalog: CatalogProvider,
    *,
    dep_identifier: str,
    project_id: str | None,
    mc_version: str,
) -> CatalogProject | None:
    """Locate the Modrinth project for a dep, by captured id else by search.

    With a captured ``project_id`` the project is fetched directly. Otherwise a
    search by ``dep_identifier`` accepts only a hit whose ``slug`` *exactly*
    equals the dep id — a Modrinth slug frequently differs from a manifest id, so
    a loose top-hit fallback could import an unrelated project. ``None`` when no
    hit's slug matches (the dep stays ``unresolvable``).

    The search is not faceted to a single loader: a server's compatible loader
    set can hold more than one loader (e.g. ``forge``/``neoforge``), and faceting
    to just one would hide projects the server can run. Authoritative loader/MC
    filtering happens in :func:`_select_import_version` over the project's
    versions.
    """

    if project_id is not None:
        return await catalog.get_project(project_id)

    result = await catalog.search(query=dep_identifier, game_version=mc_version)
    chosen = next((hit for hit in result.hits if hit.slug == dep_identifier), None)
    if chosen is None:
        return None
    return await catalog.get_project(chosen.project_id)


async def resolve_imports_from_catalog(
    plan: ResolutionPlan,
    *,
    catalog: CatalogProvider,
    server_type: str,
    mc_version: str,
    assigned: list[Mod],
) -> ResolutionPlan:
    """Enrich a plan's import-needing entries with a Modrinth ``will_import``.

    Read-only: for every ``needs_import`` or ``unresolvable`` entry it locates the
    Modrinth project (captured id else search) and selects a compatible version
    (:func:`_select_import_version`). A resolved entry becomes ``needs_import`` with
    ``will_import`` set; one Modrinth cannot satisfy becomes ``unresolvable``. The
    range dialect is the depending mod's loader (looked up per dep). A catalog
    failure for one dep leaves that entry ``unresolvable`` and does not abort the
    rest. Never imports — that is :class:`ApplyServerModResolution`.
    """

    dep_loaders = _dep_loaders(assigned)
    enriched: list[ResolutionEntry] = []
    for entry in plan.entries:
        if entry.status not in ("needs_import", "unresolvable"):
            enriched.append(entry)
            continue
        will_import = await _resolve_will_import(
            catalog,
            dep_identifier=entry.dep_identifier,
            version_range=entry.required_range,
            range_loader=dep_loaders.get(entry.dep_identifier, server_type),
            project_id=_dep_project_id(assigned, entry.dep_identifier),
            server_type=server_type,
            mc_version=mc_version,
        )
        status: ResolutionStatus = "needs_import" if will_import else "unresolvable"
        enriched.append(
            ResolutionEntry(
                entry.dep_identifier,
                entry.required_range,
                status,
                will_import=will_import,
            )
        )
    return ResolutionPlan(entries=enriched, validation=plan.validation)


def _dep_loaders(assigned: list[Mod]) -> dict[str, str]:
    """Map each required dep id to the depending mod's loader (range dialect)."""

    loaders: dict[str, str] = {}
    for mod in assigned:
        for dep in mod.dependencies:
            target = dep.get("mod_identifier")
            if isinstance(target, str) and target not in loaders:
                loaders[target] = mod.loader_type
    return loaders


async def _resolve_will_import(
    catalog: CatalogProvider,
    *,
    dep_identifier: str,
    version_range: str,
    range_loader: str,
    project_id: str | None,
    server_type: str,
    mc_version: str,
) -> WillImport | None:
    """Resolve one dep to a ``WillImport``, swallowing catalog failures as ``None``.

    Isolates a per-dep Modrinth lookup failure so it never aborts the whole plan.
    """

    try:
        project = await _locate_project(
            catalog,
            dep_identifier=dep_identifier,
            project_id=project_id,
            mc_version=mc_version,
        )
    except CatalogError:
        return None
    if project is None:
        return None
    version = _select_import_version(
        project,
        server_type=server_type,
        mc_version=mc_version,
        version_range=version_range,
        range_loader=range_loader,
    )
    if version is None:
        return None
    return WillImport(
        project_id=project.project_id,
        version_id=version.version_id,
        slug=project.slug,
        version_number=version.version_number,
    )


# ---------------------------------------------------------------------------
# Transitive closure walk + conflict detection (#1296)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FrontierDep:
    """One dependency edge queued for resolution at a given depth."""

    identifier: str
    version_range: str
    loader: str
    """The depending mod's loader, for range evaluation (C1 dialect)."""
    project_id: str | None
    """A Modrinth project id captured on the edge, if any (C3 direct lookup)."""
    depth: int
    required_by: str | None
    """The id whose resolution surfaced this edge (``None`` at depth 0)."""


def _frontier_from_mod(
    mod: Mod, depth: int, required_by: str | None
) -> list[_FrontierDep]:
    """The required (non-conflict) dep edges of ``mod`` as frontier entries."""

    frontier: list[_FrontierDep] = []
    for dep in mod.dependencies:
        if not dep.get("required") or dep.get("conflict"):
            continue
        target = dep.get("mod_identifier")
        if not isinstance(target, str):
            continue
        raw_range = dep.get("version_range")
        version_range = raw_range if isinstance(raw_range, str) else ""
        project_id = dep.get("project_id")
        frontier.append(
            _FrontierDep(
                identifier=target,
                version_range=version_range,
                loader=mod.loader_type,
                project_id=project_id if isinstance(project_id, str) else None,
                depth=depth,
                required_by=required_by,
            )
        )
    return frontier


def _conflict_edges(mods: list[Mod]) -> list[tuple[str, str]]:
    """``(declaring_id, target_id)`` pairs for every ``conflict`` dep edge."""

    edges: list[tuple[str, str]] = []
    for mod in mods:
        for dep in mod.dependencies:
            if not dep.get("conflict"):
                continue
            target = dep.get("mod_identifier")
            if isinstance(target, str):
                edges.append((mod.mod_identifier, target))
    return edges


async def resolve_closure(
    *,
    server_type: str,
    mc_version: str,
    assigned: list[Mod],
    library: list[Mod],
    catalog: CatalogProvider,
) -> ResolutionPlan:
    """Resolve the full transitive closure of a server's required deps (#1296).

    Walks deps-of-deps breadth-first from the assigned set: at each level every
    unvisited required dep is classified against the closure-so-far + library
    (:func:`_best_candidate`), and a ``needs_import`` is enriched with a Modrinth
    ``will_import`` (:func:`_resolve_will_import`). A resolved dep is added to the
    closure and its OWN required deps are queued at the next depth, so the plan
    covers transitively-required mods. A library pick expands through the chosen
    jar's manifest ``dependencies``; a Modrinth ``will_import`` expands through the
    selected version's catalog dependency edges.

    Bounded and cycle-safe: a dep id is classified at most once (``visited``), so a
    cycle (A→B→A) terminates; the walk stops expanding past
    :data:`MAX_RESOLUTION_DEPTH`, beyond which a frontier dep is reported
    ``depth_exceeded`` rather than recursed.

    After the walk, :func:`_block_and_prune` runs conflict detection over the
    closure: a resolved entry whose mod would break — or be broken by — another
    mod present or added is marked ``blocked``, and the orphaned subtree that
    exists only because a blocked mod required it is pruned (``blocked`` too), so
    apply auto-adds neither. The validation block is recomputed over the surviving
    set (the orphans excluded) so transitive findings (missing, version, conflict)
    surface without an orphan skewing them.
    """

    closure: list[Mod] = list(assigned)
    entries: list[ResolutionEntry] = []
    visited: set[str] = set()
    # Every requirer (by added-id, or ``None`` for the assigned root) that pulled in
    # each dep id — a dep can have more than one requirer, but is classified once.
    # Drives orphan-of-blocked pruning: a dep survives if any requirer survives.
    requirers: dict[str, set[str | None]] = {}
    frontier: list[_FrontierDep] = []
    for mod in assigned:
        frontier.extend(_frontier_from_mod(mod, depth=0, required_by=None))

    while frontier:
        next_frontier: list[_FrontierDep] = []
        provided = _provided_versions(closure)
        for dep in frontier:
            requirers.setdefault(dep.identifier, set()).add(dep.required_by)
            if dep.identifier in visited:
                continue
            visited.add(dep.identifier)

            present = provided.get(dep.identifier)
            if present is not None and version_satisfies(
                present, dep.version_range, dep.loader
            ):
                entries.append(
                    ResolutionEntry(
                        dep.identifier,
                        dep.version_range,
                        "already_satisfied",
                        depth=dep.depth,
                        required_by=dep.required_by,
                    )
                )
                continue

            if dep.depth >= MAX_RESOLUTION_DEPTH:
                entries.append(
                    ResolutionEntry(
                        dep.identifier,
                        dep.version_range,
                        "depth_exceeded",
                        depth=dep.depth,
                        required_by=dep.required_by,
                    )
                )
                continue

            entry, resolved_mod, resolved_will = await _classify_frontier_dep(
                dep,
                server_type=server_type,
                mc_version=mc_version,
                assigned=closure,
                library=library,
                catalog=catalog,
            )
            entries.append(entry)

            # Expand the resolved dep: a library pick through its manifest deps, a
            # Modrinth pick through its version's catalog deps. The closure grows
            # so the next level sees the new id as provided (cycle termination).
            if resolved_mod is not None:
                closure.append(resolved_mod)
                next_frontier.extend(
                    _frontier_from_mod(
                        resolved_mod,
                        depth=dep.depth + 1,
                        required_by=resolved_mod.mod_identifier,
                    )
                )
            elif resolved_will is not None:
                next_frontier.extend(
                    await _frontier_from_will_import(
                        resolved_will,
                        catalog=catalog,
                        depth=dep.depth + 1,
                        loader=dep.loader,
                    )
                )
        frontier = next_frontier

    entries, validation_mods = _block_and_prune(
        entries, assigned=assigned, requirers=requirers
    )
    validation = validate_mod_set(
        server_type=server_type, mc_version=mc_version, mods=validation_mods
    )
    return ResolutionPlan(entries=entries, validation=validation)


async def _classify_frontier_dep(
    dep: _FrontierDep,
    *,
    server_type: str,
    mc_version: str,
    assigned: list[Mod],
    library: list[Mod],
    catalog: CatalogProvider,
) -> tuple[ResolutionEntry, Mod | None, WillImport | None]:
    """Classify one frontier dep; return its entry plus what it resolved to.

    Mirrors the per-dep logic of :func:`resolve_dependencies` +
    :func:`resolve_imports_from_catalog`, but for a single edge at a known depth.
    Returns ``(entry, library_mod, will_import)``: at most one of the latter two
    is set, and identifies what to expand next.
    """

    required = _RequiredDep(dep.identifier, dep.version_range, dep.loader)
    candidate = _best_candidate(
        required, server_type=server_type, mc_version=mc_version, library=library
    )
    if candidate is not None:
        present = _provided_versions(assigned).get(dep.identifier)
        replaces = (
            _stale_providers(assigned, dep.identifier, candidate)
            if present is not None
            else []
        )
        entry = ResolutionEntry(
            dep.identifier,
            dep.version_range,
            "resolvable_from_library",
            mod=candidate,
            replaces=replaces,
            depth=dep.depth,
            required_by=dep.required_by,
        )
        return entry, candidate, None

    will_import = await _resolve_will_import(
        catalog,
        dep_identifier=dep.identifier,
        version_range=dep.version_range,
        range_loader=dep.loader,
        project_id=dep.project_id,
        server_type=server_type,
        mc_version=mc_version,
    )
    status: ResolutionStatus = "needs_import" if will_import else "unresolvable"
    entry = ResolutionEntry(
        dep.identifier,
        dep.version_range,
        status,
        will_import=will_import,
        depth=dep.depth,
        required_by=dep.required_by,
    )
    return entry, None, will_import


async def _frontier_from_will_import(
    will: WillImport,
    *,
    catalog: CatalogProvider,
    depth: int,
    loader: str,
) -> list[_FrontierDep]:
    """The required deps of a Modrinth ``will_import`` version as frontier entries.

    A catalog dependency edge carries only a ``project_id`` (no manifest id or
    range), so each required edge is queued keyed by that ``project_id``: the next
    level resolves it through the same Modrinth path (direct project lookup). A
    catalog failure yields no expansion — the rest of the closure still resolves.
    """

    try:
        version = await catalog.get_version(will.version_id)
    except CatalogError:
        return []
    frontier: list[_FrontierDep] = []
    for cdep in version.dependencies:
        if cdep.dependency_type != "required" or cdep.project_id is None:
            continue
        frontier.append(
            _FrontierDep(
                identifier=cdep.project_id,
                version_range="",
                loader=loader,
                project_id=cdep.project_id,
                depth=depth,
                required_by=will.slug,
            )
        )
    return frontier


def _block_and_prune(
    entries: list[ResolutionEntry],
    *,
    assigned: list[Mod],
    requirers: dict[str, set[str | None]],
) -> tuple[list[ResolutionEntry], list[Mod]]:
    """Block conflicting adds and prune the orphans they leave behind (#1296).

    Two reasons an added entry must not be auto-added:

    * **Conflict** — the mod it would add either declares a ``conflict`` edge
      against an id present/added, or is itself the target of such an edge from
      another present/added mod.
    * **Orphan-of-blocked** — it exists in the closure *only* because a blocked
      mod required it: none of its requirers (``requirers`` maps each dep id to
      every requirer that pulled it in) is the assigned root or a surviving
      (non-blocked) added entry. A dep required by both a blocked and a surviving
      mod keeps a valid requirer and is not pruned.

    Blocking one mod and pruning its subtree changes which ids are present, which
    can change conflict detection for other mods (a pruned dep may have been
    someone's conflict target). So this iterates to a fixpoint: detect conflicts
    over the surviving set, mark them blocked, prune the orphans-of-blocked,
    recompute the present set, and repeat until the blocked set stops growing. The
    blocked set only grows and the closure is finite, so the loop terminates.

    Returns the entries (with ``blocked`` set on every conflicting/orphaned one)
    and the mod set for the validation block. Conflict detection excludes *all*
    blocked entries (an orphan must not pollute ``present_ids``), but the
    validation set keeps the conflict-blocked picks — so the conflict that caused
    the block still surfaces to the user — while dropping the silent orphans.
    """

    assigned_ids: set[str] = set()
    for mod in assigned:
        assigned_ids.add(mod.mod_identifier)
        assigned_ids.update(mod.provides)

    conflict_blocked: set[int] = set()
    orphan_blocked: set[int] = set()
    while True:
        blocked = conflict_blocked | orphan_blocked
        present_ids = _present_ids(entries, assigned_ids, blocked)
        edges = _conflict_edges(_surviving_mods(entries, assigned, blocked))
        broken_ids = {target for _d, target in edges if target in present_ids}
        breaking_ids = {decl for decl, target in edges if target in present_ids}
        conflict_ids = broken_ids | breaking_ids

        added_ids = {
            added_id
            for idx, entry in enumerate(entries)
            if idx not in blocked and (added_id := _entry_added_id(entry)) is not None
        }

        new_conflict: set[int] = set()
        new_orphan: set[int] = set()
        for idx, entry in enumerate(entries):
            if idx in blocked:
                continue
            added_id = _entry_added_id(entry)
            if added_id is None:
                continue
            # A direct conflict participant is blocked outright.
            if added_id in conflict_ids:
                new_conflict.add(idx)
                continue
            # Otherwise it survives only if some requirer survives: the assigned
            # root (``None``), an assigned id, or a still-added entry's id. A dep
            # shared by a blocked and a surviving requirer keeps a valid requirer.
            edge_requirers = requirers.get(entry.dep_identifier, {entry.required_by})
            if any(
                req is None or req in assigned_ids or req in added_ids
                for req in edge_requirers
            ):
                continue
            new_orphan.add(idx)

        if not new_conflict and not new_orphan:
            break
        conflict_blocked |= new_conflict
        orphan_blocked |= new_orphan

    blocked = conflict_blocked | orphan_blocked
    # Validation keeps the conflict-blocked picks (so the conflict reports) but
    # not the orphans (their requirer is gone — reporting them would be noise).
    validation_mods = _surviving_mods(entries, assigned, orphan_blocked)
    return _mark_blocked(entries, blocked), validation_mods


def _present_ids(
    entries: list[ResolutionEntry],
    assigned_ids: set[str],
    blocked: set[int],
) -> set[str]:
    """Every id present if the plan applies, excluding the blocked entries.

    A blocked entry adds nothing, so its id is left out — neither a conflict-blocked
    pick nor an orphan pollutes the conflict-detection present set.
    """

    present_ids: set[str] = set(assigned_ids)
    for idx, entry in enumerate(entries):
        if idx in blocked:
            continue
        if entry.status == "resolvable_from_library" and entry.mod is not None:
            present_ids.add(entry.mod.mod_identifier)
            present_ids.update(entry.mod.provides)
        elif entry.status == "needs_import" and entry.will_import is not None:
            present_ids.add(entry.will_import.slug)
    return present_ids


def _surviving_mods(
    entries: list[ResolutionEntry],
    assigned: list[Mod],
    excluded: set[int],
) -> list[Mod]:
    """The assigned mods plus every library pick whose entry is not ``excluded``."""

    mods: list[Mod] = list(assigned)
    for idx, entry in enumerate(entries):
        if idx in excluded:
            continue
        if entry.status == "resolvable_from_library" and entry.mod is not None:
            mods.append(entry.mod)
    return mods


def _mark_blocked(
    entries: list[ResolutionEntry], blocked: set[int]
) -> list[ResolutionEntry]:
    """A copy of ``entries`` with ``blocked`` set on every index in ``blocked``."""

    return [
        _replace_entry_blocked(entry) if idx in blocked else entry
        for idx, entry in enumerate(entries)
    ]


def _entry_added_id(entry: ResolutionEntry) -> str | None:
    """The id a resolved entry would add, or ``None`` if it adds nothing."""

    if entry.status == "resolvable_from_library" and entry.mod is not None:
        return entry.mod.mod_identifier
    if entry.status == "needs_import" and entry.will_import is not None:
        return entry.will_import.slug
    return None


def _replace_entry_blocked(entry: ResolutionEntry) -> ResolutionEntry:
    """A copy of ``entry`` with ``blocked`` set (frozen dataclasses are immutable)."""

    return ResolutionEntry(
        entry.dep_identifier,
        entry.required_range,
        entry.status,
        mod=entry.mod,
        replaces=entry.replaces,
        will_import=entry.will_import,
        depth=entry.depth,
        required_by=entry.required_by,
        blocked=True,
    )


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
    """Plan a server's dependency resolution against the library + Modrinth.

    Walks the full transitive closure of the assigned set's required deps (#1296):
    each dep is classified against the library (#1294) and, when import-needing,
    enriched with a concrete Modrinth ``will_import`` candidate (#1295); a resolved
    dep's own deps are then walked too (bounded, cycle-safe). A resolution that
    would introduce a conflict is marked ``blocked``. Read-only end to end: it only
    queries the catalog — no import.
    """

    uow: UnitOfWork
    catalog: CatalogProvider

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> ResolutionPlan:
        server_type, mc_version, assigned, library = await _load_resolution_inputs(
            self.uow, community_id, server_id
        )
        return await resolve_closure(
            server_type=server_type,
            mc_version=mc_version,
            assigned=assigned,
            library=library,
            catalog=self.catalog,
        )


@dataclass(frozen=True)
class ApplyServerModResolution:
    """Apply a server's resolvable deps from the library + Modrinth, then re-plan.

    Plans against the current library and Modrinth, then, behind one explicit call:

    * assigns every ``resolvable_from_library`` pick through :class:`AssignMods`
      (which holds the lifecycle lock and is at-rest gated — a running server
      raises ``ServerFilesUnsettledError``);
    * imports each ``needs_import`` ``will_import`` version into the library via
      :class:`ImportMod` (sha256-dedup, so a jar already present is reused) and
      assigns it (#1295).

    Idempotent: with nothing newly resolvable both lists are empty and the returned
    plan shows the picks ``already_satisfied``.

    A pick that carries ``replaces`` (the id is present but out of range — a
    ``version_unsatisfied`` finding) is a swap, not an add: the stale
    same-``mod_identifier`` assignment(s) are unassigned via :class:`UnassignMod`
    before the in-range library mod is assigned, so exactly one in-range version
    of the id remains and the re-plan converges to ``already_satisfied``.

    A per-dep Modrinth lookup/import failure is isolated: that dep's id is added to
    the returned ``failed`` list and the remaining deps still apply. The
    lifecycle/at-rest gate is NOT swallowed — a running server aborts the whole
    apply (it raises ``ServerFilesUnsettledError`` from the gated assign, which the
    edge maps to 409).
    """

    uow: UnitOfWork
    assign_mods: AssignMods
    unassign_mod: UnassignMod
    import_mod: ImportMod
    catalog: CatalogProvider

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        applied_by: uuid.UUID,
    ) -> tuple[ResolutionPlan, list[ModId], list[str]]:
        server_type, mc_version, assigned, library = await _load_resolution_inputs(
            self.uow, community_id, server_id
        )
        plan = await resolve_closure(
            server_type=server_type,
            mc_version=mc_version,
            assigned=assigned,
            library=library,
            catalog=self.catalog,
        )

        # De-dup the picks: two deps can resolve to the same library mod. Collect
        # the stale assignments each replacement supersedes so they are unassigned
        # before the new version is added (never the chosen mod itself). A
        # ``blocked`` entry would introduce a conflict — it is reported but never
        # auto-added (#1296).
        picked: dict[ModId, None] = {}
        replaced: dict[ModId, None] = {}
        for entry in plan.entries:
            if entry.blocked:
                continue
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

        # Import + assign each Modrinth ``will_import`` dep. Per-dep failures are
        # isolated: a Modrinth lookup/import error for one dep is recorded and the
        # rest still apply. De-dup so two deps resolving to the same version import
        # once. The assign holds the same at-rest gate as the library picks, so a
        # running server still aborts the apply (after the first download) rather
        # than mutating the working set.
        imported: dict[str, ModId] = {}
        failed: list[str] = []
        for entry in plan.entries:
            wi = entry.will_import
            if wi is None or entry.blocked:
                continue
            if wi.version_id in imported:
                continue
            # Per-dep isolation covers the *import* failures (catalog unreachable,
            # bad/oversized/tampered jar). The lifecycle gate errors
            # (``ServerFilesUnsettledError`` / ``ServerBusyError``) from the assign
            # are NOT swallowed — they must abort the whole apply with a 409.
            try:
                mod = await self.import_mod(
                    project_id=wi.project_id,
                    version_id=wi.version_id,
                    imported_by=applied_by,
                )
            except (
                CatalogError,
                InvalidModJarError,
                FileTooLargeError,
                ModIntegrityError,
                ValueError,
            ) as exc:
                failed.append(entry.dep_identifier)
                _LOGGER.warning(
                    "modrinth import failed for dep %s (%s@%s): %s",
                    entry.dep_identifier,
                    wi.project_id,
                    wi.version_id,
                    exc,
                )
                continue
            # Identity guard: a matching slug does not guarantee the jar's manifest
            # id matches the dep. The imported jar must actually provide the dep id
            # (its own ``mod_identifier`` or a ``provides`` alias); otherwise an
            # unrelated mod could be assigned silently. Record it as a failed import
            # and never assign it.
            if not _provides(mod, entry.dep_identifier):
                failed.append(entry.dep_identifier)
                _LOGGER.warning(
                    "modrinth import for dep %s (%s@%s) provides %s, not the dep id"
                    " — refusing to assign",
                    entry.dep_identifier,
                    wi.project_id,
                    wi.version_id,
                    mod.mod_identifier,
                )
                continue
            await self.assign_mods(
                community_id=community_id,
                server_id=server_id,
                mod_ids=[mod.id],
                assigned_by=applied_by,
            )
            imported[wi.version_id] = mod.id
            applied.append(mod.id)

        new_plan = await ResolveServerMods(self.uow, self.catalog)(
            community_id=community_id, server_id=server_id
        )
        return new_plan, applied, failed
