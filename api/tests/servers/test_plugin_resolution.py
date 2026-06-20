"""Unit tests for plugin dependency auto-resolution (issue #1309, phase C).

Re-fit of mod-management's resolution (#1258) to the per-server model: there is
no library, so a required dependency is either ``already_satisfied`` by the
server's installed set or ``needs_import`` from Modrinth. Tests cover the pure
classifier, the Modrinth catalog enrichment, the transitive closure walk with
conflict blocking, and the plan/apply use cases.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import uuid
import zipfile

import pytest

from mc_server_dashboard_api.servers.application.catalog import InstallFromCatalog
from mc_server_dashboard_api.servers.application.plugin_resolution import (
    ApplyPluginResolution,
    ResolvePluginDependencies,
    resolve_dependencies,
    resolve_imports_from_catalog,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogDependency,
    CatalogFile,
    CatalogProject,
    CatalogVersion,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    ServerFilesUnsettledError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.fakes import (
    FakeCatalogProvider,
    FakeClock,
    FakeFileStore,
    FakePluginCacheStore,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())


def _server(
    *,
    server_type: ServerType = ServerType.FABRIC,
    mc_version: str = "1.20.4",
    running: bool = False,
) -> Server:
    state = ObservedState.RUNNING if running else ObservedState.STOPPED
    desired = DesiredState.RUNNING if running else DesiredState.STOPPED
    return Server(
        id=ServerId.new(),
        community_id=_COMMUNITY,
        name=ServerName("test-server"),
        mc_edition="java",
        mc_version=mc_version,
        server_type=server_type,
        execution_backend=ExecutionBackend.CONTAINER,
        config={},
        desired_state=desired,
        observed_state=state,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _dep(
    mod_identifier: str,
    *,
    required: bool = True,
    version_range: str = "",
    conflict: bool = False,
    project_id: str | None = None,
) -> dict[str, object]:
    dep: dict[str, object] = {
        "mod_identifier": mod_identifier,
        "version_range": version_range,
        "required": required,
        "conflict": conflict,
    }
    if project_id is not None:
        dep["project_id"] = project_id
    return dep


def _plugin(
    *,
    server_id: ServerId,
    mod_identifier: str | None = "mod-a",
    provides: list[str] | None = None,
    version_number: str = "1.0.0",
    dependencies: list[dict[str, object]] | None = None,
    mc_versions: list[str] | None = None,
) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path=f"mods/{mod_identifier or 'plugin'}.jar",
        filename=f"{mod_identifier or 'plugin'}.jar",
        display_name=mod_identifier or "plugin",
        description=None,
        loader_type=LoaderType.MOD,
        source=PluginSource.LOCAL,
        source_project_id=None,
        source_version_id=None,
        version_number=version_number,
        checksum_sha512=None,
        sha256=None,
        size_bytes=10,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
        mod_identifier=mod_identifier,
        provides=provides or [],
        dependencies=dependencies or [],
        mc_versions=mc_versions if mc_versions is not None else ["1.20.4"],
    )


def _fabric_jar(
    *, mod_id: str, version: str = "0.92.0", depends: dict[str, str] | None = None
) -> bytes:
    """A jar whose ``fabric.mod.json`` declares ``mod_id`` (and optional depends).

    The apply path installs the jar and re-parses its manifest, so the re-plan
    only recognizes the imported dep as ``already_satisfied`` when the jar's
    declared id matches the dep identifier.
    """

    manifest: dict[str, object] = {"id": mod_id, "version": version}
    if depends:
        manifest["depends"] = depends
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("fabric.mod.json", json.dumps(manifest))
    return buf.getvalue()


def _project(
    *,
    project_id: str = "FABRICAPI",
    slug: str = "fabric-api",
    title: str = "Fabric API",
) -> CatalogProject:
    return CatalogProject(
        project_id=project_id,
        slug=slug,
        title=title,
        description="A core library",
        body="",
        author="FabricMC",
        icon_url=None,
        downloads=1_000_000,
        categories=["library"],
        game_versions=["1.20.4"],
        loaders=["fabric"],
    )


def _version(
    *,
    version_id: str = "VER1",
    version_number: str = "0.92.0",
    filename: str = "fabric-api-0.92.0.jar",
    file_content: bytes = b"fabric-api-jar",
    game_versions: list[str] | None = None,
    dependencies: list[CatalogDependency] | None = None,
) -> CatalogVersion:
    return CatalogVersion(
        version_id=version_id,
        version_number=version_number,
        name=f"Fabric API {version_number}",
        game_versions=game_versions or ["1.20.4"],
        loaders=["fabric"],
        files=[
            CatalogFile(
                url=f"https://cdn.modrinth.com/data/{filename}",
                filename=filename,
                size=len(file_content),
                sha512=hashlib.sha512(file_content).hexdigest(),
                primary=True,
            )
        ],
        date_published="2024-01-15T12:00:00Z",
        dependencies=dependencies or [],
    )


# ---------------------------------------------------------------------------
# resolve_dependencies (pure)
# ---------------------------------------------------------------------------


class TestResolveDependencies:
    def test_no_plugins_yields_empty_plan(self) -> None:
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.20.4", plugins=[]
        )
        assert plan.entries == []

    def test_already_satisfied_when_present_in_set_in_range(self) -> None:
        sid = ServerId.new()
        a = _plugin(
            server_id=sid,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api", version_range=">=0.90.0")],
        )
        fabric = _plugin(
            server_id=sid, mod_identifier="fabric-api", version_number="0.92.0"
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.20.4", plugins=[a, fabric]
        )
        entry = next(e for e in plan.entries if e.dep_identifier == "fabric-api")
        assert entry.status == "already_satisfied"

    def test_already_satisfied_via_provides_alias(self) -> None:
        sid = ServerId.new()
        a = _plugin(
            server_id=sid,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api")],
        )
        host = _plugin(server_id=sid, mod_identifier="qsl", provides=["fabric-api"])
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.20.4", plugins=[a, host]
        )
        entry = next(e for e in plan.entries if e.dep_identifier == "fabric-api")
        assert entry.status == "already_satisfied"

    def test_missing_dep_needs_import(self) -> None:
        sid = ServerId.new()
        a = _plugin(
            server_id=sid,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api")],
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.20.4", plugins=[a]
        )
        entry = next(e for e in plan.entries if e.dep_identifier == "fabric-api")
        assert entry.status == "needs_import"

    def test_present_out_of_range_needs_import(self) -> None:
        sid = ServerId.new()
        a = _plugin(
            server_id=sid,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api", version_range=">=0.95.0")],
        )
        fabric = _plugin(
            server_id=sid, mod_identifier="fabric-api", version_number="0.92.0"
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.20.4", plugins=[a, fabric]
        )
        entry = next(e for e in plan.entries if e.dep_identifier == "fabric-api")
        assert entry.status == "needs_import"

    def test_optional_dep_is_not_resolved(self) -> None:
        sid = ServerId.new()
        a = _plugin(
            server_id=sid,
            mod_identifier="mod-a",
            dependencies=[_dep("optional-lib", required=False)],
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.20.4", plugins=[a]
        )
        assert plan.entries == []

    def test_conflict_edge_is_not_a_required_dep(self) -> None:
        sid = ServerId.new()
        a = _plugin(
            server_id=sid,
            mod_identifier="mod-a",
            dependencies=[_dep("rival", required=True, conflict=True)],
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.20.4", plugins=[a]
        )
        assert all(e.dep_identifier != "rival" for e in plan.entries)

    def test_validation_block_echoed(self) -> None:
        sid = ServerId.new()
        a = _plugin(
            server_id=sid,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api")],
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.20.4", plugins=[a]
        )
        assert any(m.depends_on == "fabric-api" for m in plan.validation.missing_deps)


# ---------------------------------------------------------------------------
# resolve_imports_from_catalog (read-only Modrinth enrichment)
# ---------------------------------------------------------------------------


class TestResolveImportsFromCatalog:
    async def test_resolves_needs_import_via_search_fallback(self) -> None:
        sid = ServerId.new()
        a = _plugin(
            server_id=sid,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api")],
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.20.4", plugins=[a]
        )
        catalog = FakeCatalogProvider()
        catalog.seed_project(_project(), versions=[_version()])

        enriched = await resolve_imports_from_catalog(
            plan,
            catalog=catalog,
            server_type="fabric",
            mc_version="1.20.4",
            plugins=[a],
        )
        entry = next(e for e in enriched.entries if e.dep_identifier == "fabric-api")
        assert entry.status == "needs_import"
        assert entry.will_import is not None
        assert entry.will_import.project_id == "FABRICAPI"
        assert entry.will_import.version_id == "VER1"

    async def test_resolves_via_captured_project_id(self) -> None:
        sid = ServerId.new()
        a = _plugin(
            server_id=sid,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api", project_id="FABRICAPI")],
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.20.4", plugins=[a]
        )
        catalog = FakeCatalogProvider()
        # Seed only by project id -- a search would not match (no slug seeded for
        # search), so resolution must use the captured project_id directly.
        catalog.seed_project(_project(slug="different-slug"), versions=[_version()])

        enriched = await resolve_imports_from_catalog(
            plan,
            catalog=catalog,
            server_type="fabric",
            mc_version="1.20.4",
            plugins=[a],
        )
        entry = next(e for e in enriched.entries if e.dep_identifier == "fabric-api")
        assert entry.status == "needs_import"
        assert entry.will_import is not None
        assert entry.will_import.project_id == "FABRICAPI"

    async def test_picks_newest_satisfying_version(self) -> None:
        sid = ServerId.new()
        a = _plugin(
            server_id=sid,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api", project_id="FABRICAPI")],
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.20.4", plugins=[a]
        )
        catalog = FakeCatalogProvider()
        catalog.seed_project(
            _project(),
            versions=[
                _version(version_id="OLD", version_number="0.90.0"),
                _version(version_id="NEW", version_number="0.92.0"),
            ],
        )

        enriched = await resolve_imports_from_catalog(
            plan,
            catalog=catalog,
            server_type="fabric",
            mc_version="1.20.4",
            plugins=[a],
        )
        entry = next(e for e in enriched.entries if e.dep_identifier == "fabric-api")
        assert entry.will_import is not None
        assert entry.will_import.version_id == "NEW"

    async def test_stays_unresolvable_when_only_out_of_range(self) -> None:
        sid = ServerId.new()
        a = _plugin(
            server_id=sid,
            mod_identifier="mod-a",
            dependencies=[
                _dep("fabric-api", version_range=">=0.95.0", project_id="FABRICAPI")
            ],
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.20.4", plugins=[a]
        )
        catalog = FakeCatalogProvider()
        catalog.seed_project(_project(), versions=[_version(version_number="0.92.0")])

        enriched = await resolve_imports_from_catalog(
            plan,
            catalog=catalog,
            server_type="fabric",
            mc_version="1.20.4",
            plugins=[a],
        )
        entry = next(e for e in enriched.entries if e.dep_identifier == "fabric-api")
        assert entry.status == "unresolvable"
        assert entry.will_import is None

    async def test_no_exact_slug_match_stays_unresolvable(self) -> None:
        sid = ServerId.new()
        a = _plugin(
            server_id=sid,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api")],
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.20.4", plugins=[a]
        )
        catalog = FakeCatalogProvider()
        # Only an unrelated-slug project is in the catalog -- no exact slug match.
        catalog.seed_project(
            _project(project_id="OTHER", slug="some-other-mod"),
            versions=[_version()],
        )

        enriched = await resolve_imports_from_catalog(
            plan,
            catalog=catalog,
            server_type="fabric",
            mc_version="1.20.4",
            plugins=[a],
        )
        entry = next(e for e in enriched.entries if e.dep_identifier == "fabric-api")
        assert entry.status == "unresolvable"

    async def test_per_dep_catalog_failure_is_isolated(self) -> None:
        sid = ServerId.new()
        a = _plugin(
            server_id=sid,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api", project_id="FABRICAPI")],
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.20.4", plugins=[a]
        )
        catalog = FakeCatalogProvider(unavailable=True)

        enriched = await resolve_imports_from_catalog(
            plan,
            catalog=catalog,
            server_type="fabric",
            mc_version="1.20.4",
            plugins=[a],
        )
        entry = next(e for e in enriched.entries if e.dep_identifier == "fabric-api")
        assert entry.status == "unresolvable"


# ---------------------------------------------------------------------------
# ResolvePluginDependencies + transitive closure
# ---------------------------------------------------------------------------


def _seed(uow: FakeUnitOfWork, server: Server, *plugins: ServerPlugin) -> None:
    uow.servers.seed(server)
    for plugin in plugins:
        uow.plugins.seed(plugin)


class TestResolvePluginDependencies:
    async def test_plan_server_not_found(self) -> None:
        uow = FakeUnitOfWork()
        uc = ResolvePluginDependencies(uow, FakeCatalogProvider())
        with pytest.raises(ServerNotFoundError):
            await uc(community_id=_COMMUNITY, server_id=ServerId.new())

    async def test_plan_classifies_direct_dep(self) -> None:
        server = _server()
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api", project_id="FABRICAPI")],
        )
        _seed(uow, server, a)
        catalog = FakeCatalogProvider()
        catalog.seed_project(_project(), versions=[_version()])

        plan = await ResolvePluginDependencies(uow, catalog)(
            community_id=_COMMUNITY, server_id=server.id
        )
        by_id = {e.dep_identifier: e for e in plan.entries}
        assert by_id["fabric-api"].status == "needs_import"
        assert by_id["fabric-api"].depth == 0

    async def test_already_satisfied_yields_no_import(self) -> None:
        server = _server()
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api")],
        )
        fabric = _plugin(
            server_id=server.id,
            mod_identifier="fabric-api",
            version_number="0.92.0",
        )
        _seed(uow, server, a, fabric)
        catalog = FakeCatalogProvider()

        plan = await ResolvePluginDependencies(uow, catalog)(
            community_id=_COMMUNITY, server_id=server.id
        )
        by_id = {e.dep_identifier: e for e in plan.entries}
        assert by_id["fabric-api"].status == "already_satisfied"
        assert by_id["fabric-api"].will_import is None

    async def test_transitive_modrinth_chain(self) -> None:
        # mod-a -> fabric-api (Modrinth); the selected fabric-api version carries a
        # required catalog dep on DEEPLIB, which must surface at depth 1.
        server = _server()
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api", project_id="FABRICAPI")],
        )
        _seed(uow, server, a)
        catalog = FakeCatalogProvider()
        fa_version = _version(
            dependencies=[
                CatalogDependency(
                    version_id=None,
                    project_id="DEEPLIB",
                    dependency_type="required",
                )
            ]
        )
        catalog.seed_project(_project(), versions=[fa_version])
        catalog.seed_project(
            _project(project_id="DEEPLIB", slug="deeplib", title="Deep Lib"),
            versions=[
                _version(
                    version_id="DEEPVER",
                    version_number="1.0.0",
                    filename="deeplib-1.0.0.jar",
                )
            ],
        )

        plan = await ResolvePluginDependencies(uow, catalog)(
            community_id=_COMMUNITY, server_id=server.id
        )
        by_id = {e.dep_identifier: e for e in plan.entries}
        assert by_id["fabric-api"].status == "needs_import"
        assert by_id["fabric-api"].depth == 0
        deep = by_id["DEEPLIB"]
        assert deep.status == "needs_import"
        assert deep.depth == 1
        assert deep.required_by == "fabric-api"
        assert deep.will_import is not None
        assert deep.will_import.project_id == "DEEPLIB"

    async def test_cycle_terminates(self) -> None:
        # mod-a -> mod-b and mod-b -> mod-a, both installed: a cycle that must
        # terminate (both already satisfied, nothing to import).
        server = _server()
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[_dep("mod-b")],
        )
        b = _plugin(
            server_id=server.id,
            mod_identifier="mod-b",
            dependencies=[_dep("mod-a")],
        )
        _seed(uow, server, a, b)

        plan = await ResolvePluginDependencies(uow, FakeCatalogProvider())(
            community_id=_COMMUNITY, server_id=server.id
        )
        statuses = {e.dep_identifier: e.status for e in plan.entries}
        assert statuses["mod-a"] == "already_satisfied"
        assert statuses["mod-b"] == "already_satisfied"

    async def test_transitive_conflict_blocks_the_import(self) -> None:
        # mod-a -> fabric-api (Modrinth); installed "rival" declares a conflict
        # with fabric-api, so importing fabric-api would conflict: blocked.
        server = _server()
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api", project_id="FABRICAPI")],
        )
        rival = _plugin(
            server_id=server.id,
            mod_identifier="rival",
            dependencies=[_dep("fabric-api", conflict=True)],
        )
        _seed(uow, server, a, rival)
        catalog = FakeCatalogProvider()
        catalog.seed_project(_project(), versions=[_version()])

        plan = await ResolvePluginDependencies(uow, catalog)(
            community_id=_COMMUNITY, server_id=server.id
        )
        entry = next(e for e in plan.entries if e.dep_identifier == "fabric-api")
        assert entry.blocked is True

    async def test_blocked_chain_is_pruned(self) -> None:
        # mod-a -> fabric-api (blocked by rival); fabric-api -> DEEPLIB. DEEPLIB
        # only exists because blocked fabric-api needs it, so it is pruned.
        server = _server()
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api", project_id="FABRICAPI")],
        )
        rival = _plugin(
            server_id=server.id,
            mod_identifier="rival",
            dependencies=[_dep("fabric-api", conflict=True)],
        )
        _seed(uow, server, a, rival)
        catalog = FakeCatalogProvider()
        fa_version = _version(
            dependencies=[
                CatalogDependency(
                    version_id=None,
                    project_id="DEEPLIB",
                    dependency_type="required",
                )
            ]
        )
        catalog.seed_project(_project(), versions=[fa_version])
        catalog.seed_project(
            _project(project_id="DEEPLIB", slug="deeplib", title="Deep Lib"),
            versions=[
                _version(
                    version_id="DEEPVER",
                    version_number="1.0.0",
                    filename="deeplib.jar",
                )
            ],
        )

        plan = await ResolvePluginDependencies(uow, catalog)(
            community_id=_COMMUNITY, server_id=server.id
        )
        by_id = {e.dep_identifier: e for e in plan.entries}
        assert by_id["fabric-api"].blocked is True
        assert by_id["DEEPLIB"].blocked is True

    async def test_surviving_import_keeps_transitive_dep_when_slug_differs(
        self,
    ) -> None:
        # mod-a -> fabric-api (captured project_id, so the Modrinth slug differs
        # from the requested dep id); the selected version -> DEEPLIB. No conflict,
        # so fabric-api survives and DEEPLIB must NOT be pruned. The transitive
        # child carries required_by = parent.slug, so orphan-pruning must key the
        # surviving import's added id off its slug, not the requested dep id --
        # otherwise DEEPLIB is wrongly marked blocked and dropped on apply.
        server = _server()
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api", project_id="FABRICAPI")],
        )
        _seed(uow, server, a)
        catalog = FakeCatalogProvider()
        fa_version = _version(
            dependencies=[
                CatalogDependency(
                    version_id=None,
                    project_id="DEEPLIB",
                    dependency_type="required",
                )
            ]
        )
        # The Modrinth project's slug ("fabric-api-modrinth") differs from the
        # requested dep id ("fabric-api"): the project_id-captured path.
        catalog.seed_project(
            _project(slug="fabric-api-modrinth"), versions=[fa_version]
        )
        catalog.seed_project(
            _project(project_id="DEEPLIB", slug="deeplib", title="Deep Lib"),
            versions=[
                _version(
                    version_id="DEEPVER",
                    version_number="1.0.0",
                    filename="deeplib.jar",
                )
            ],
        )

        plan = await ResolvePluginDependencies(uow, catalog)(
            community_id=_COMMUNITY, server_id=server.id
        )
        by_id = {e.dep_identifier: e for e in plan.entries}
        assert by_id["fabric-api"].blocked is False
        assert by_id["DEEPLIB"].status == "needs_import"
        assert by_id["DEEPLIB"].blocked is False

    async def test_apply_installs_full_closure_when_slug_differs(self) -> None:
        # Apply counterpart of the test above: with the parent's slug != dep id,
        # the surviving import AND its transitive child must both install -- the
        # exact failure (a plugin left with a missing required dep) this feature
        # exists to prevent.
        server = _server()
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api", project_id="FABRICAPI")],
        )
        _seed(uow, server, a)
        catalog = FakeCatalogProvider()
        fa_jar = _fabric_jar(mod_id="fabric-api")
        fa_version = _version(
            file_content=fa_jar,
            dependencies=[
                CatalogDependency(
                    version_id=None,
                    project_id="DEEPLIB",
                    dependency_type="required",
                )
            ],
        )
        catalog.seed_project(
            _project(slug="fabric-api-modrinth"), versions=[fa_version]
        )
        _seed_downloadable(catalog, fa_version, fa_jar)
        deep_jar = _fabric_jar(mod_id="deeplib")
        deep_version = _version(
            version_id="DEEPVER",
            version_number="1.0.0",
            filename="deeplib.jar",
            file_content=deep_jar,
        )
        catalog.seed_project(
            _project(project_id="DEEPLIB", slug="deeplib", title="Deep Lib"),
            versions=[deep_version],
        )
        _seed_downloadable(catalog, deep_version, deep_jar)
        file_store = FakeFileStore()
        cache = FakePluginCacheStore()
        install = _install_uc(uow, catalog, file_store, cache)

        _new_plan, installed, failed = await ApplyPluginResolution(
            uow, catalog, install
        )(
            community_id=_COMMUNITY,
            server_id=server.id,
            applied_by=uuid.uuid4(),
        )
        assert failed == []
        assert {p.source_project_id for p in installed} == {"FABRICAPI", "DEEPLIB"}

    async def test_unresolvable_when_modrinth_has_nothing(self) -> None:
        server = _server()
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[_dep("missing-lib")],
        )
        _seed(uow, server, a)

        plan = await ResolvePluginDependencies(uow, FakeCatalogProvider())(
            community_id=_COMMUNITY, server_id=server.id
        )
        entry = next(e for e in plan.entries if e.dep_identifier == "missing-lib")
        assert entry.status == "unresolvable"


# ---------------------------------------------------------------------------
# ApplyPluginResolution
# ---------------------------------------------------------------------------


def _install_uc(
    uow: FakeUnitOfWork,
    catalog: FakeCatalogProvider,
    file_store: FakeFileStore,
    cache: FakePluginCacheStore,
) -> InstallFromCatalog:
    return InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=file_store,
        cache=cache,
        clock=FakeClock(_NOW),
    )


def _seed_downloadable(
    catalog: FakeCatalogProvider, version: CatalogVersion, content: bytes
) -> None:
    """Seed the version's file bytes; ``content`` must match the file's sha512."""

    for file in version.files:
        assert file.sha512 == hashlib.sha512(content).hexdigest()
        catalog.seed_file(file.url, content)


class TestApplyPluginResolution:
    async def test_apply_installs_needs_import(self) -> None:
        server = _server()
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api", project_id="FABRICAPI")],
        )
        _seed(uow, server, a)
        catalog = FakeCatalogProvider()
        jar = _fabric_jar(mod_id="fabric-api")
        version = _version(file_content=jar)
        catalog.seed_project(_project(), versions=[version])
        _seed_downloadable(catalog, version, jar)
        file_store = FakeFileStore()
        cache = FakePluginCacheStore()
        install = _install_uc(uow, catalog, file_store, cache)

        new_plan, installed, failed = await ApplyPluginResolution(
            uow, catalog, install
        )(
            community_id=_COMMUNITY,
            server_id=server.id,
            applied_by=uuid.uuid4(),
        )
        assert failed == []
        assert len(installed) == 1
        assert installed[0].source_project_id == "FABRICAPI"
        # Re-plan: fabric-api is now installed, so it is already satisfied.
        by_id = {e.dep_identifier: e for e in new_plan.entries}
        assert by_id["fabric-api"].status == "already_satisfied"

    async def test_apply_is_idempotent_when_already_satisfied(self) -> None:
        server = _server()
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api")],
        )
        fabric = _plugin(
            server_id=server.id,
            mod_identifier="fabric-api",
            version_number="0.92.0",
        )
        _seed(uow, server, a, fabric)
        catalog = FakeCatalogProvider()
        file_store = FakeFileStore()
        cache = FakePluginCacheStore()
        install = _install_uc(uow, catalog, file_store, cache)

        _new_plan, installed, failed = await ApplyPluginResolution(
            uow, catalog, install
        )(
            community_id=_COMMUNITY,
            server_id=server.id,
            applied_by=uuid.uuid4(),
        )
        assert installed == []
        assert failed == []

    async def test_apply_at_rest_gate_blocks_running_server(self) -> None:
        server = _server(running=True)
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api", project_id="FABRICAPI")],
        )
        _seed(uow, server, a)
        catalog = FakeCatalogProvider()
        jar = _fabric_jar(mod_id="fabric-api")
        version = _version(file_content=jar)
        catalog.seed_project(_project(), versions=[version])
        _seed_downloadable(catalog, version, jar)
        file_store = FakeFileStore()
        cache = FakePluginCacheStore()
        install = _install_uc(uow, catalog, file_store, cache)

        with pytest.raises(ServerFilesUnsettledError):
            await ApplyPluginResolution(uow, catalog, install)(
                community_id=_COMMUNITY,
                server_id=server.id,
                applied_by=uuid.uuid4(),
            )

    async def test_apply_per_dep_failure_isolated(self) -> None:
        # Two needs_import deps; one has no downloadable bytes (install fails),
        # the other installs. The failure is isolated and recorded.
        server = _server()
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[
                _dep("fabric-api", project_id="FABRICAPI"),
                _dep("broken-lib", project_id="BROKEN"),
            ],
        )
        _seed(uow, server, a)
        catalog = FakeCatalogProvider()
        jar = _fabric_jar(mod_id="fabric-api")
        good = _version(file_content=jar)
        catalog.seed_project(_project(), versions=[good])
        _seed_downloadable(catalog, good, jar)
        # broken-lib resolves to a version whose file bytes are NOT seeded -> the
        # download raises during install and is isolated.
        broken = _version(
            version_id="BROKENVER",
            version_number="1.0.0",
            filename="broken.jar",
        )
        catalog.seed_project(
            _project(project_id="BROKEN", slug="broken-lib", title="Broken"),
            versions=[broken],
        )
        file_store = FakeFileStore()
        cache = FakePluginCacheStore()
        install = _install_uc(uow, catalog, file_store, cache)

        _new_plan, installed, failed = await ApplyPluginResolution(
            uow, catalog, install
        )(
            community_id=_COMMUNITY,
            server_id=server.id,
            applied_by=uuid.uuid4(),
        )
        assert [p.source_project_id for p in installed] == ["FABRICAPI"]
        assert failed == ["broken-lib"]

    async def test_apply_does_not_install_blocked(self) -> None:
        server = _server()
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[_dep("fabric-api", project_id="FABRICAPI")],
        )
        rival = _plugin(
            server_id=server.id,
            mod_identifier="rival",
            dependencies=[_dep("fabric-api", conflict=True)],
        )
        _seed(uow, server, a, rival)
        catalog = FakeCatalogProvider()
        jar = _fabric_jar(mod_id="fabric-api")
        version = _version(file_content=jar)
        catalog.seed_project(_project(), versions=[version])
        _seed_downloadable(catalog, version, jar)
        file_store = FakeFileStore()
        cache = FakePluginCacheStore()
        install = _install_uc(uow, catalog, file_store, cache)

        _new_plan, installed, failed = await ApplyPluginResolution(
            uow, catalog, install
        )(
            community_id=_COMMUNITY,
            server_id=server.id,
            applied_by=uuid.uuid4(),
        )
        assert installed == []
        assert failed == []

    async def test_apply_installs_two_versions_of_the_same_project(self) -> None:
        # Apply dedups planned imports by version_id, NOT project_id: two deps that
        # resolve to the same project but DIFFERENT versions must both install. Two
        # distinct dep ids capture the same project_id "LIB"; their version ranges
        # select different versions (V1 vs V2).
        server = _server()
        uow = FakeUnitOfWork()
        a = _plugin(
            server_id=server.id,
            mod_identifier="mod-a",
            dependencies=[
                _dep("lib-old", version_range="<=1.0.0", project_id="LIB"),
                _dep("lib-new", version_range=">=2.0.0", project_id="LIB"),
            ],
        )
        _seed(uow, server, a)
        catalog = FakeCatalogProvider()
        jar_v1 = _fabric_jar(mod_id="lib", version="1.0.0")
        jar_v2 = _fabric_jar(mod_id="lib", version="2.0.0")
        v1 = _version(
            version_id="V1",
            version_number="1.0.0",
            filename="lib-1.0.0.jar",
            file_content=jar_v1,
        )
        v2 = _version(
            version_id="V2",
            version_number="2.0.0",
            filename="lib-2.0.0.jar",
            file_content=jar_v2,
        )
        catalog.seed_project(
            _project(project_id="LIB", slug="lib", title="Lib"),
            versions=[v1, v2],
        )
        _seed_downloadable(catalog, v1, jar_v1)
        _seed_downloadable(catalog, v2, jar_v2)
        file_store = FakeFileStore()
        cache = FakePluginCacheStore()
        install = _install_uc(uow, catalog, file_store, cache)

        _new_plan, installed, failed = await ApplyPluginResolution(
            uow, catalog, install
        )(
            community_id=_COMMUNITY,
            server_id=server.id,
            applied_by=uuid.uuid4(),
        )
        assert failed == []
        # Both versions installed: same project_id, distinct version_ids.
        assert [p.source_project_id for p in installed] == ["LIB", "LIB"]
        assert {p.source_version_id for p in installed} == {"V1", "V2"}
