"""Auto-resolve a server's missing required dependencies (issue #1309, phase C).

Turns the phase-B validation checklist (:mod:`plugin_validation`) into
auto-resolution. Where mod-management (#1258) resolved against a global mod
**library** + an assigned subset, the per-server model has **no library**: the
"set" the deps are resolved against is the server's **own installed plugin
set**. So a required dependency is either already satisfied by an installed
plugin, or it must be imported from Modrinth.

The result is a **plan** -- a classification of every required dependency in the
transitive closure -- that the edge can either show
(``POST .../plugins/resolve``, read-only) or apply
(``POST .../plugins/resolve/apply``, which installs the ``needs_import`` picks
via :class:`InstallFromCatalog`).

Classification of each required dependency:

* ``already_satisfied`` -- the installed set already provides the id at a version
  in range (the dep is not a finding). Re-running after apply yields this, so
  apply is idempotent.
* ``needs_import`` -- the id is missing from (or unsatisfiable by) the installed
  set, and Modrinth has a version compatible with the server's loader, MC
  version, and the required range. The concrete ``will_import``
  (project@version) is resolved by the catalog enrichment and installed on apply.
* ``unresolvable`` -- the id is missing and Modrinth has nothing compatible.
* ``depth_exceeded`` -- the dep sits past :data:`MAX_RESOLUTION_DEPTH` in the
  transitive walk; reported rather than recursed so a pathological graph can
  never loop or blow the stack.

Transitive closure (:func:`resolve_closure`):

* The walk is breadth-first from the installed set. A resolved ``needs_import``
  dep's OWN required deps -- the catalog dependency edges of the selected version
  -- are walked at the next depth, so the plan covers transitively-required mods.
* It is cycle-safe and bounded: each dep id is classified at most once (a cycle
  A->B->A terminates), and the walk stops expanding past
  :data:`MAX_RESOLUTION_DEPTH`. Each entry carries its ``depth`` and
  ``required_by`` so the transitive chain is visible.
* Conflict detection spans the whole closure: a resolved dep whose mod would
  break -- or be broken by -- a plugin present or being added is marked
  ``blocked``, and the subtree that exists *only* because a blocked mod required
  it is pruned (also ``blocked``). Apply never installs a blocked entry.

Per-server catalog re-fit: mod-management expanded a Modrinth pick through a
``get_version`` call; the per-server :class:`CatalogProvider` has no such method,
so a ``will_import``'s transitive deps are walked through ``list_versions`` (the
selected version's ``dependencies`` carry the depended-on ``project_id``). A
catalog dependency edge is keyed by that ``project_id`` and resolved at the next
level by a direct project lookup.

Pure: :func:`resolve_dependencies` does no I/O.
:func:`resolve_imports_from_catalog` and :func:`resolve_closure` do read-only
catalog I/O. The use cases load the data and, for apply, delegate the mutation to
:class:`InstallFromCatalog`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Literal

from mc_server_dashboard_api.servers.application.catalog import InstallFromCatalog
from mc_server_dashboard_api.servers.application.catalog_deps import (
    installed_project_ids,
    required_catalog_deps,
)
from mc_server_dashboard_api.servers.application.plugin_validation import (
    PluginValidation,
    _loader_dialect,
    validate_plugin_set,
)
from mc_server_dashboard_api.servers.application.version_range import (
    compare_versions,
    version_satisfies,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogProject,
    CatalogProvider,
    CatalogVersion,
)
from mc_server_dashboard_api.servers.domain.errors import (
    CatalogChecksumMismatchError,
    CatalogProjectNotFoundError,
    CatalogUnavailableError,
    FileTooLargeError,
    InvalidFilePathError,
    PluginAlreadyExistsError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    ServerPlugin,
    modrinth_loader_for_server_type,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
    ServerType,
)

_LOGGER = logging.getLogger(__name__)

ResolutionStatus = Literal[
    "already_satisfied",
    "needs_import",
    "unresolvable",
    "depth_exceeded",
]

#: How deep the transitive dependency walk recurses before it stops expanding
#: the frontier. A deeper-than-this dep is reported ``depth_exceeded`` rather
#: than resolved, so a pathological graph can never loop or blow the stack.
MAX_RESOLUTION_DEPTH = 10

# Modrinth's per-catalog-version dependency classification we treat as required.
_REQUIRED_DEP_TYPE = "required"

# Catalog errors a per-dep Modrinth lookup may raise; swallowed for isolation so
# one bad dependency never aborts the whole plan.
_CATALOG_ERRORS = (CatalogUnavailableError, CatalogProjectNotFoundError)


@dataclass(frozen=True)
class WillImport:
    """The concrete Modrinth project@version a ``needs_import`` dep resolves to.

    ``project_id`` / ``version_id`` drive the install on apply; ``slug`` /
    ``version_number`` are for the human-readable preview.
    """

    project_id: str
    version_id: str
    slug: str
    version_number: str


@dataclass(frozen=True)
class ResolutionEntry:
    """One required dependency and how it can be resolved."""

    dep_identifier: str
    """The required dependency's target ``mod_identifier`` (or Modrinth id)."""
    required_range: str
    """The required ``version_range`` (empty == any)."""
    status: ResolutionStatus
    """How the dependency classifies against the installed set and Modrinth."""
    will_import: WillImport | None = None
    """The Modrinth project@version a ``needs_import`` dep resolves to.

    Set for a ``needs_import`` entry when Modrinth has a compatible version;
    ``None`` otherwise. On apply this version is installed onto the server."""
    depth: int = 0
    """How far from the installed set this dep sits in the transitive walk.

    ``0`` is a direct required dep of an installed plugin; ``1`` is a dep of a
    plugin resolved at depth 0; and so on. The walk stops expanding past
    :data:`MAX_RESOLUTION_DEPTH`."""
    required_by: str | None = None
    """The id whose resolution surfaced this dep, or ``None`` for a depth-0 dep."""
    blocked: bool = False
    """Whether importing this resolved dep would introduce a conflict.

    A ``needs_import`` entry is blocked when the mod it would add breaks -- or is
    broken by -- a plugin already present or being added elsewhere in the
    closure. Apply skips a blocked entry: the user must resolve the conflict by
    hand."""


@dataclass(frozen=True)
class ResolutionPlan:
    """A server's dependency-resolution plan plus its validation findings."""

    entries: list[ResolutionEntry] = field(default_factory=list)
    validation: PluginValidation = field(default_factory=PluginValidation)


@dataclass(frozen=True)
class _RequiredDep:
    """A direct required dependency, keyed for dedup across the installed set."""

    identifier: str
    version_range: str
    project_id: str | None
    """A Modrinth project id captured on the dep edge, if any (direct lookup)."""


def _required_deps(plugins: list[ServerPlugin]) -> list[_RequiredDep]:
    """The distinct direct required deps of the installed set, in first-seen order.

    Two plugins declaring the same ``(id, range)`` collapse to one entry. A
    ``conflict`` edge is not a requirement and is skipped here (it is handled by
    conflict detection).
    """

    seen: set[tuple[str, str]] = set()
    deps: list[_RequiredDep] = []
    for plugin in plugins:
        if not plugin.mod_identifier:
            continue
        for dep in plugin.dependencies:
            if not dep.get("required") or dep.get("conflict"):
                continue
            target = dep.get("mod_identifier")
            if not isinstance(target, str):
                continue
            raw_range = dep.get("version_range")
            version_range = raw_range if isinstance(raw_range, str) else ""
            key = (target, version_range)
            if key in seen:
                continue
            seen.add(key)
            deps.append(_RequiredDep(target, version_range, _dep_project_id(dep)))
    return deps


def _dep_project_id(dep: dict[str, object]) -> str | None:
    """A Modrinth ``project_id`` captured on a manifest/catalog dep edge, if any."""

    project_id = dep.get("project_id")
    return project_id if isinstance(project_id, str) and project_id else None


def _provided_versions(plugins: list[ServerPlugin]) -> dict[str, str]:
    """Map every id the installed set satisfies to a providing plugin's version."""

    provided: dict[str, str] = {}
    for plugin in plugins:
        if not plugin.mod_identifier:
            continue
        version = plugin.version_number or ""
        provided[plugin.mod_identifier] = version
        for pid in plugin.provides:
            provided[pid] = version
    return provided


def resolve_dependencies(
    *,
    server_type: str,
    mc_version: str,
    plugins: list[ServerPlugin],
) -> ResolutionPlan:
    """Classify each direct required dep of the installed set (pure, no I/O).

    Without a library, a dep is either ``already_satisfied`` by the installed set
    or ``needs_import`` (the catalog enrichment then resolves a concrete
    ``will_import`` or downgrades it to ``unresolvable``). The validation block is
    the same :func:`validate_plugin_set` the edge already shows, recomputed here
    so the plan echoes the current findings.
    """

    loader = _loader_dialect(server_type)
    provided = _provided_versions(plugins)
    entries: list[ResolutionEntry] = []
    for dep in _required_deps(plugins):
        present = provided.get(dep.identifier)
        if present is not None and version_satisfies(
            present, dep.version_range, loader
        ):
            entries.append(
                ResolutionEntry(dep.identifier, dep.version_range, "already_satisfied")
            )
            continue
        entries.append(
            ResolutionEntry(dep.identifier, dep.version_range, "needs_import")
        )

    validation = validate_plugin_set(
        server_type=server_type, mc_version=mc_version, plugins=plugins
    )
    return ResolutionPlan(entries=entries, validation=validation)


# ---------------------------------------------------------------------------
# Modrinth catalog enrichment
# ---------------------------------------------------------------------------


def _select_import_version(
    versions: list[CatalogVersion],
    *,
    version_range: str,
    range_loader: str,
) -> CatalogVersion | None:
    """The newest catalog version that satisfies the required range.

    ``versions`` are already filtered by the server's loader and MC version (the
    catalog ``list_versions`` facets), so only the required ``version_range`` is
    re-checked here, in the depending plugin's loader dialect. The newest
    ``version_number`` wins; the ``version_id`` breaks ties for determinism.
    ``None`` when no version qualifies.
    """

    candidates = [
        version
        for version in versions
        if version.files
        and version_satisfies(version.version_number, version_range, range_loader)
    ]
    if not candidates:
        return None
    version_key = cmp_to_key(compare_versions)
    return max(
        candidates,
        key=lambda v: (version_key(v.version_number), v.version_id),
    )


async def _locate_project(
    catalog: CatalogProvider,
    *,
    dep_identifier: str,
    project_id: str | None,
    loader: str,
    mc_version: str,
) -> CatalogProject | None:
    """Locate the Modrinth project for a dep, by captured id else by search.

    With a captured ``project_id`` the project is fetched directly. Otherwise a
    search by ``dep_identifier`` accepts only a hit whose ``slug`` *exactly*
    equals the dep id -- a Modrinth slug frequently differs from a manifest id, so
    a loose top-hit fallback could import an unrelated project. ``None`` when no
    hit's slug matches (the dep stays ``unresolvable``).
    """

    if project_id is not None:
        return await catalog.get_project(project_id)

    result = await catalog.search(
        query=dep_identifier, loader=loader, game_versions=[mc_version]
    )
    chosen = next((hit for hit in result.hits if hit.slug == dep_identifier), None)
    if chosen is None:
        return None
    return await catalog.get_project(chosen.project_id)


async def _resolve_will_import(
    catalog: CatalogProvider,
    *,
    dep_identifier: str,
    version_range: str,
    range_loader: str,
    project_id: str | None,
    loader: str,
    mc_version: str,
) -> WillImport | None:
    """Resolve one dep to a ``WillImport``, swallowing catalog failures as ``None``.

    Locates the Modrinth project (captured id else exact-slug search), lists its
    versions compatible with the server loader + MC, and selects the newest
    satisfying the required range. Isolates a per-dep Modrinth failure so it never
    aborts the whole plan.
    """

    try:
        project = await _locate_project(
            catalog,
            dep_identifier=dep_identifier,
            project_id=project_id,
            loader=loader,
            mc_version=mc_version,
        )
        if project is None:
            return None
        versions = await catalog.list_versions(
            project.project_id, loader=loader, game_versions=[mc_version]
        )
    except _CATALOG_ERRORS:
        return None
    version = _select_import_version(
        versions, version_range=version_range, range_loader=range_loader
    )
    if version is None:
        return None
    return WillImport(
        project_id=project.project_id,
        version_id=version.version_id,
        slug=project.slug,
        version_number=version.version_number,
    )


async def resolve_imports_from_catalog(
    plan: ResolutionPlan,
    *,
    catalog: CatalogProvider,
    server_type: str,
    mc_version: str,
    plugins: list[ServerPlugin],
) -> ResolutionPlan:
    """Enrich a plan's ``needs_import`` entries with a Modrinth ``will_import``.

    Read-only: for every ``needs_import`` or ``unresolvable`` entry it locates the
    Modrinth project and selects a compatible version. A resolved entry stays
    ``needs_import`` with ``will_import`` set; one Modrinth cannot satisfy becomes
    ``unresolvable``. Never imports -- that is :class:`ApplyPluginResolution`.
    """

    loader = modrinth_loader_for_server_type(ServerType(server_type))
    range_loader = _loader_dialect(server_type)
    dep_projects = _dep_project_ids(plugins)
    enriched: list[ResolutionEntry] = []
    for entry in plan.entries:
        if entry.status not in ("needs_import", "unresolvable"):
            enriched.append(entry)
            continue
        will_import = await _resolve_will_import(
            catalog,
            dep_identifier=entry.dep_identifier,
            version_range=entry.required_range,
            range_loader=range_loader,
            project_id=dep_projects.get(entry.dep_identifier),
            loader=loader,
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


def _dep_project_ids(plugins: list[ServerPlugin]) -> dict[str, str]:
    """Map each required dep id to a captured Modrinth ``project_id``, if any."""

    projects: dict[str, str] = {}
    for plugin in plugins:
        for dep in plugin.dependencies:
            target = dep.get("mod_identifier")
            project_id = _dep_project_id(dep)
            if isinstance(target, str) and project_id and target not in projects:
                projects[target] = project_id
    return projects


# ---------------------------------------------------------------------------
# Transitive closure walk + conflict detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FrontierDep:
    """One dependency edge queued for resolution at a given depth."""

    identifier: str
    version_range: str
    project_id: str | None
    """A Modrinth project id captured on the edge, if any (direct lookup)."""
    depth: int
    required_by: str | None
    """The id whose resolution surfaced this edge (``None`` at depth 0)."""


def _frontier_from_plugin(
    plugin: ServerPlugin, depth: int, required_by: str | None
) -> list[_FrontierDep]:
    """The required (non-conflict) dep edges of ``plugin`` as frontier entries."""

    if not plugin.mod_identifier:
        return []
    frontier: list[_FrontierDep] = []
    for dep in plugin.dependencies:
        if not dep.get("required") or dep.get("conflict"):
            continue
        target = dep.get("mod_identifier")
        if not isinstance(target, str):
            continue
        raw_range = dep.get("version_range")
        version_range = raw_range if isinstance(raw_range, str) else ""
        frontier.append(
            _FrontierDep(
                identifier=target,
                version_range=version_range,
                project_id=_dep_project_id(dep),
                depth=depth,
                required_by=required_by,
            )
        )
    return frontier


def _catalog_frontier(
    plugins: list[ServerPlugin], manifest_frontier: list[_FrontierDep]
) -> list[_FrontierDep]:
    """Depth-0 frontier entries for installed Modrinth plugins' catalog deps (#1321).

    Each required catalog dep is keyed by ``project_id`` -- the same key the
    transitive walk (:func:`_frontier_from_will_import`) uses -- so the closure
    machinery resolves and imports it by a direct project lookup, dedups it
    against a transitive dep on the same project, and the slug≠id orphan-pruning
    keeps working unchanged.

    Dedup against the manifest frontier and the installed set: a dep whose
    project is already installed (by ``source_project_id``) or already queued by a
    manifest dep that captured the same ``project_id`` is skipped, so a project
    needed via both a manifest dep and a catalog dep is planned/imported once.
    """

    present = installed_project_ids(plugins)
    manifest_project_ids = {f.project_id for f in manifest_frontier if f.project_id}
    seen: set[str] = set()
    frontier: list[_FrontierDep] = []
    for plugin in plugins:
        for dep in required_catalog_deps(plugin):
            if (
                dep.project_id in present
                or dep.project_id in manifest_project_ids
                or dep.project_id in seen
            ):
                continue
            seen.add(dep.project_id)
            frontier.append(
                _FrontierDep(
                    identifier=dep.project_id,
                    version_range="",
                    project_id=dep.project_id,
                    depth=0,
                    required_by=None,
                )
            )
    return frontier


def _conflict_edges(plugins: list[ServerPlugin]) -> list[tuple[str, str]]:
    """``(declaring_id, target_id)`` pairs for every ``conflict`` dep edge."""

    edges: list[tuple[str, str]] = []
    for plugin in plugins:
        if not plugin.mod_identifier:
            continue
        for dep in plugin.dependencies:
            if not dep.get("conflict"):
                continue
            target = dep.get("mod_identifier")
            if isinstance(target, str):
                edges.append((plugin.mod_identifier, target))
    return edges


async def resolve_closure(
    *,
    server_type: str,
    mc_version: str,
    plugins: list[ServerPlugin],
    catalog: CatalogProvider,
) -> ResolutionPlan:
    """Resolve the full transitive closure of a server's required deps.

    Walks deps-of-deps breadth-first from the installed set: at each level every
    unvisited required dep is classified against the closure-so-far, and a missing
    one is enriched with a Modrinth ``will_import`` (:func:`_resolve_will_import`).
    A resolved import's own required deps -- the selected catalog version's
    dependency edges -- are queued at the next depth, so the plan covers
    transitively-required mods.

    Bounded and cycle-safe: a dep id is classified at most once, so a cycle
    (A->B->A) terminates; the walk stops expanding past
    :data:`MAX_RESOLUTION_DEPTH`, beyond which a frontier dep is reported
    ``depth_exceeded``.

    After the walk, :func:`_block_and_prune` runs conflict detection over the
    closure: a resolved entry whose mod would break -- or be broken by -- another
    plugin present or added is marked ``blocked``, and the orphaned subtree that
    exists only because a blocked mod required it is pruned (``blocked`` too).
    """

    loader = modrinth_loader_for_server_type(ServerType(server_type))
    range_loader = _loader_dialect(server_type)

    entries: list[ResolutionEntry] = []
    # Each dep id is classified at most once: a cycle (A->B->A) terminates, and a
    # dep required by two plugins is resolved once. ``provided`` (the installed
    # set's ids) is the only already-satisfied source -- an import resolved this
    # run is not re-required by a deeper dep without going through ``visited``.
    visited: set[str] = set()
    # Every requirer (by added-id, or ``None`` for an installed root) that pulled
    # in each dep id; drives orphan-of-blocked pruning.
    requirers: dict[str, set[str | None]] = {}
    provided = _provided_versions(plugins)

    frontier: list[_FrontierDep] = []
    for plugin in plugins:
        frontier.extend(_frontier_from_plugin(plugin, depth=0, required_by=None))
    frontier.extend(_catalog_frontier(plugins, frontier))

    while frontier:
        next_frontier: list[_FrontierDep] = []
        for dep in frontier:
            requirers.setdefault(dep.identifier, set()).add(dep.required_by)
            if dep.identifier in visited:
                continue
            visited.add(dep.identifier)

            present = provided.get(dep.identifier)
            if present is not None and version_satisfies(
                present, dep.version_range, range_loader
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

            will_import = await _resolve_will_import(
                catalog,
                dep_identifier=dep.identifier,
                version_range=dep.version_range,
                range_loader=range_loader,
                project_id=dep.project_id,
                loader=loader,
                mc_version=mc_version,
            )
            status: ResolutionStatus = "needs_import" if will_import else "unresolvable"
            entries.append(
                ResolutionEntry(
                    dep.identifier,
                    dep.version_range,
                    status,
                    will_import=will_import,
                    depth=dep.depth,
                    required_by=dep.required_by,
                )
            )
            if will_import is not None:
                next_frontier.extend(
                    await _frontier_from_will_import(
                        will_import,
                        catalog=catalog,
                        loader=loader,
                        mc_version=mc_version,
                        depth=dep.depth + 1,
                    )
                )
        frontier = next_frontier

    entries = _block_and_prune(entries, plugins=plugins, requirers=requirers)
    validation = validate_plugin_set(
        server_type=server_type, mc_version=mc_version, plugins=plugins
    )
    return ResolutionPlan(entries=entries, validation=validation)


async def _frontier_from_will_import(
    will: WillImport,
    *,
    catalog: CatalogProvider,
    loader: str,
    mc_version: str,
    depth: int,
) -> list[_FrontierDep]:
    """The required deps of a Modrinth ``will_import`` version as frontier entries.

    The per-server :class:`CatalogProvider` has no ``get_version``, so the
    selected version's catalog dependency edges are read from a re-list of the
    project's versions. Each required edge carries a ``project_id`` (the
    depended-on project): the next level resolves it through the same Modrinth
    path. A catalog failure yields no expansion -- the rest of the closure still
    resolves.
    """

    try:
        versions = await catalog.list_versions(
            will.project_id, loader=loader, game_versions=[mc_version]
        )
    except _CATALOG_ERRORS:
        return []
    version = next((v for v in versions if v.version_id == will.version_id), None)
    if version is None:
        return []
    frontier: list[_FrontierDep] = []
    for cdep in version.dependencies:
        if cdep.dependency_type != _REQUIRED_DEP_TYPE:
            continue
        frontier.append(
            _FrontierDep(
                identifier=cdep.project_id,
                version_range="",
                project_id=cdep.project_id,
                depth=depth,
                required_by=will.slug,
            )
        )
    return frontier


def _block_and_prune(
    entries: list[ResolutionEntry],
    *,
    plugins: list[ServerPlugin],
    requirers: dict[str, set[str | None]],
) -> list[ResolutionEntry]:
    """Block conflicting imports and prune the orphans they leave behind.

    Two reasons an import entry must not be auto-added:

    * **Conflict** -- the mod it would add either declares a ``conflict`` edge
      against an id present/added, or is itself the target of such an edge from
      another present/added plugin.
    * **Orphan-of-blocked** -- it exists in the closure *only* because a blocked
      mod required it: none of its requirers is an installed root or a surviving
      (non-blocked) import.

    Blocking one mod and pruning its subtree changes which ids are present, which
    can change conflict detection for other mods, so this iterates to a fixpoint.
    Returns the entries with ``blocked`` set on every conflicting/orphaned one.
    """

    installed_ids: set[str] = set()
    installed_conflict_edges = _conflict_edges(plugins)
    for plugin in plugins:
        if plugin.mod_identifier:
            installed_ids.add(plugin.mod_identifier)
            installed_ids.update(plugin.provides)

    blocked: set[int] = set()
    while True:
        present_ids = _present_ids(entries, installed_ids, blocked)
        edges = list(installed_conflict_edges)
        broken_ids = {target for _d, target in edges if target in present_ids}
        breaking_ids = {decl for decl, target in edges if target in present_ids}
        conflict_ids = broken_ids | breaking_ids

        added_ids = {
            added_id
            for idx, entry in enumerate(entries)
            if idx not in blocked and (added_id := _entry_added_id(entry)) is not None
        }

        new_blocked: set[int] = set()
        for idx, entry in enumerate(entries):
            if idx in blocked:
                continue
            added_id = _entry_added_id(entry)
            if added_id is None:
                continue
            if added_id in conflict_ids:
                new_blocked.add(idx)
                continue
            edge_requirers = requirers.get(entry.dep_identifier, {entry.required_by})
            if any(
                req is None or req in installed_ids or req in added_ids
                for req in edge_requirers
            ):
                continue
            new_blocked.add(idx)

        if not new_blocked:
            break
        blocked |= new_blocked

    return _mark_blocked(entries, blocked)


def _present_ids(
    entries: list[ResolutionEntry],
    installed_ids: set[str],
    blocked: set[int],
) -> set[str]:
    """Every id present if the plan applies, excluding the blocked entries."""

    present_ids: set[str] = set(installed_ids)
    for idx, entry in enumerate(entries):
        if idx in blocked:
            continue
        if entry.status == "needs_import" and entry.will_import is not None:
            present_ids.add(entry.will_import.slug)
            present_ids.add(entry.dep_identifier)
    return present_ids


def _entry_added_id(entry: ResolutionEntry) -> str | None:
    """The id a resolved entry would add, or ``None`` if it adds nothing."""

    if entry.status == "needs_import" and entry.will_import is not None:
        return entry.will_import.slug
    return None


def _mark_blocked(
    entries: list[ResolutionEntry], blocked: set[int]
) -> list[ResolutionEntry]:
    """A copy of ``entries`` with ``blocked`` set on every index in ``blocked``."""

    return [
        _replace_entry_blocked(entry) if idx in blocked else entry
        for idx, entry in enumerate(entries)
    ]


def _replace_entry_blocked(entry: ResolutionEntry) -> ResolutionEntry:
    """A copy of ``entry`` with ``blocked`` set (frozen dataclasses are immutable)."""

    return ResolutionEntry(
        entry.dep_identifier,
        entry.required_range,
        entry.status,
        will_import=entry.will_import,
        depth=entry.depth,
        required_by=entry.required_by,
        blocked=True,
    )


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


async def _load_resolution_inputs(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> tuple[str, str, list[ServerPlugin]]:
    """Load (server_type, mc_version, installed plugins) for resolution."""

    async with uow:
        server = await uow.servers.get_by_id(server_id)
        if server is None or server.community_id != community_id:
            raise ServerNotFoundError(str(server_id.value))
        plugins = await uow.plugins.list_for_server(server_id)
    return server.server_type.value, server.mc_version, plugins


@dataclass(frozen=True)
class ResolvePluginDependencies:
    """Plan a server's dependency resolution against its set + Modrinth.

    Walks the full transitive closure of the installed set's required deps: each
    dep is classified ``already_satisfied`` (present in range) or, when missing,
    enriched with a concrete Modrinth ``will_import`` candidate; a resolved
    import's own deps are then walked too (bounded, cycle-safe). A resolution that
    would introduce a conflict is marked ``blocked``. Read-only end to end: it
    only queries the catalog -- no install.
    """

    uow: UnitOfWork
    catalog: CatalogProvider

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> ResolutionPlan:
        server_type, mc_version, plugins = await _load_resolution_inputs(
            self.uow, community_id, server_id
        )
        return await resolve_closure(
            server_type=server_type,
            mc_version=mc_version,
            plugins=plugins,
            catalog=self.catalog,
        )


@dataclass(frozen=True)
class ApplyPluginResolution:
    """Install a server's resolvable deps from Modrinth, then re-plan.

    Plans against the current set and Modrinth, then installs each non-blocked
    ``needs_import`` ``will_import`` version onto the server via
    :class:`InstallFromCatalog` (which holds the lifecycle lock, is at-rest gated
    -- a running server raises ``ServerFilesUnsettledError`` -- parses the jar
    manifest, and dedups on the content-addressed cache).

    Idempotent: with nothing newly resolvable the picks come back
    ``already_satisfied`` and nothing is installed.

    A per-dep Modrinth lookup/install failure is isolated: that dep's id is added
    to the returned ``failed`` list and the remaining deps still apply. The
    lifecycle/at-rest gate is NOT swallowed -- a running server aborts the whole
    apply (it raises ``ServerFilesUnsettledError`` from the gated install, which
    the edge maps to 409). A ``blocked`` entry is reported but never installed.
    """

    uow: UnitOfWork
    catalog: CatalogProvider
    install_from_catalog: InstallFromCatalog

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        applied_by: uuid.UUID,
    ) -> tuple[ResolutionPlan, list[ServerPlugin], list[str]]:
        server_type, mc_version, plugins = await _load_resolution_inputs(
            self.uow, community_id, server_id
        )
        plan = await resolve_closure(
            server_type=server_type,
            mc_version=mc_version,
            plugins=plugins,
            catalog=self.catalog,
        )

        installed: list[ServerPlugin] = []
        failed: list[str] = []
        seen_versions: set[str] = set()
        for entry in plan.entries:
            wi = entry.will_import
            if wi is None or entry.blocked:
                continue
            if wi.version_id in seen_versions:
                continue
            seen_versions.add(wi.version_id)
            # Per-dep isolation covers install failures (catalog unreachable,
            # bad/oversized/tampered jar, an id already installed at this path).
            # The lifecycle gate errors (``ServerFilesUnsettledError`` /
            # ``ServerBusyError``) from the install are NOT swallowed -- they must
            # abort the whole apply with a 409.
            try:
                plugin = await self.install_from_catalog(
                    community_id=community_id,
                    server_id=server_id,
                    project_id=wi.project_id,
                    version_id=wi.version_id,
                    installed_by=applied_by,
                )
            except (
                CatalogUnavailableError,
                CatalogProjectNotFoundError,
                CatalogChecksumMismatchError,
                InvalidFilePathError,
                FileTooLargeError,
                PluginAlreadyExistsError,
            ) as exc:
                failed.append(entry.dep_identifier)
                _LOGGER.warning(
                    "modrinth install failed for dep %s (%s@%s): %s",
                    entry.dep_identifier,
                    wi.project_id,
                    wi.version_id,
                    exc,
                )
                continue
            installed.append(plugin)

        new_plan = await ResolvePluginDependencies(self.uow, self.catalog)(
            community_id=community_id, server_id=server_id
        )
        return new_plan, installed, failed
