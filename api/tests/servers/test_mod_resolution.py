"""Resolver tests for auto-resolving required deps from the library (#1294).

Two layers, both against fakes (no DB, TESTING.md Section 4):

* the pure :func:`resolve_dependencies` classifier — each dep lands in
  ``resolvable_from_library`` / ``needs_import`` / ``unresolvable`` /
  ``already_satisfied``, including a ``provides``-satisfied case and the
  version-range gate (a present-but-out-of-range library mod is NOT chosen);
* the ``ResolveServerMods`` (plan) and ``ApplyServerModResolution`` (apply) use
  cases — apply assigns the in-library picks, the server then validates clean for
  them, the at-rest gate raises while running, and re-running is idempotent.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import uuid
import zipfile

import pytest

from mc_server_dashboard_api.servers.application.mod_resolution import (
    ApplyServerModResolution,
    ResolveServerMods,
    _select_import_version,
    resolve_dependencies,
    resolve_imports_from_catalog,
)
from mc_server_dashboard_api.servers.application.mods import ImportMod
from mc_server_dashboard_api.servers.application.server_mods import (
    AssignMods,
    UnassignMod,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogProject,
    CatalogSearchHit,
    CatalogSearchResult,
    CatalogUnavailableError,
    CatalogVersion,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    ServerFilesUnsettledError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.mod import Mod, ModId, ModLoader, ModSide
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.servers.fakes import (
    FakeCatalogProvider,
    FakeClock,
    FakeFileStore,
    FakeLifecycleLock,
    FakeModStore,
    FakeServerRepository,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 19, 12, 0, 0, tzinfo=dt.timezone.utc)
_COMMUNITY_ID = CommunityId(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))


def _server(*, running: bool = False, server_type: str = "fabric") -> Server:
    state = DesiredState.RUNNING if running else DesiredState.STOPPED
    observed = ObservedState.RUNNING if running else ObservedState.STOPPED
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=_COMMUNITY_ID,
        name=ServerName("test-server"),
        mc_edition="java",
        mc_version="1.21",
        server_type=ServerType(server_type),
        execution_backend=ExecutionBackend.CONTAINER,
        config={},
        desired_state=state,
        observed_state=observed,
        observed_at=_NOW,
        assigned_worker_id=WorkerId(uuid.uuid4()) if running else None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _mod(
    *,
    mod_identifier: str,
    version_number: str = "1.0.0",
    loader_type: ModLoader = "fabric",
    mc_versions: list[str] | None = None,
    side: ModSide = "both",
    provides: list[str] | None = None,
    dependencies: list[dict[str, object]] | None = None,
) -> Mod:
    return Mod(
        id=ModId.new(),
        filename=f"{mod_identifier}.jar",
        display_name=mod_identifier,
        description=None,
        loader_type=loader_type,
        mod_identifier=mod_identifier,
        provides=provides or [],
        version_number=version_number,
        mc_versions=["1.21"] if mc_versions is None else mc_versions,
        side=side,
        dependencies=dependencies or [],
        sha256_hash=uuid.uuid4().hex,
        sha512_hash=None,
        size_bytes=4,
        source="local",
        source_project_id=None,
        source_version_id=None,
        uploaded_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _dep(
    identifier: str, *, version_range: str = "", required: bool = True
) -> dict[str, object]:
    return {
        "mod_identifier": identifier,
        "version_range": version_range,
        "required": required,
    }


# ---------------------------------------------------------------------------
# Pure resolver
# ---------------------------------------------------------------------------


class TestResolveDependencies:
    def test_no_assigned_mods_yields_empty_plan(self) -> None:
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.21", assigned=[], library=[]
        )
        assert plan.entries == []

    def test_resolvable_from_library_when_library_provides_and_fits(self) -> None:
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", version_range=">=0.90.0")],
        )
        fabric_api = _mod(mod_identifier="fabric-api", version_number="0.92.0")
        plan = resolve_dependencies(
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
            library=[fabric_api],
        )
        assert len(plan.entries) == 1
        entry = plan.entries[0]
        assert entry.dep_identifier == "fabric-api"
        assert entry.required_range == ">=0.90.0"
        assert entry.status == "resolvable_from_library"
        assert entry.mod is not None
        assert entry.mod.id == fabric_api.id

    def test_resolvable_via_provides_alias(self) -> None:
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api")],
        )
        # A library mod that does not carry "fabric-api" as its id, but provides it.
        bundle = _mod(
            mod_identifier="fabric-api-bundle",
            provides=["fabric-api"],
        )
        plan = resolve_dependencies(
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
            library=[bundle],
        )
        assert plan.entries[0].status == "resolvable_from_library"
        assert plan.entries[0].mod is not None
        assert plan.entries[0].mod.id == bundle.id

    def test_picks_highest_satisfying_version(self) -> None:
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", version_range=">=0.90.0")],
        )
        old = _mod(mod_identifier="fabric-api", version_number="0.91.0")
        new = _mod(mod_identifier="fabric-api", version_number="0.95.0")
        plan = resolve_dependencies(
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
            library=[old, new],
        )
        assert plan.entries[0].mod is not None
        assert plan.entries[0].mod.id == new.id

    def test_out_of_range_library_mod_is_not_chosen_needs_import(self) -> None:
        # The id IS present in the library, but only at a version below the range.
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", version_range=">=0.90.0")],
        )
        too_old = _mod(mod_identifier="fabric-api", version_number="0.80.0")
        plan = resolve_dependencies(
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
            library=[too_old],
        )
        assert plan.entries[0].status == "needs_import"
        assert plan.entries[0].mod is None

    def test_loader_incompatible_library_mod_needs_import(self) -> None:
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api")],
        )
        # Provides the id, but it is a forge mod a fabric server cannot run.
        forge_provider = _mod(mod_identifier="fabric-api", loader_type="forge")
        plan = resolve_dependencies(
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
            library=[forge_provider],
        )
        assert plan.entries[0].status == "needs_import"

    def test_mc_incompatible_library_mod_needs_import(self) -> None:
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api")],
        )
        wrong_mc = _mod(mod_identifier="fabric-api", mc_versions=["1.20.4"])
        plan = resolve_dependencies(
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
            library=[wrong_mc],
        )
        assert plan.entries[0].status == "needs_import"

    def test_unresolvable_when_library_has_no_provider(self) -> None:
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api")],
        )
        unrelated = _mod(mod_identifier="lithium")
        plan = resolve_dependencies(
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
            library=[unrelated],
        )
        assert plan.entries[0].status == "unresolvable"

    def test_already_satisfied_when_present_in_set_in_range(self) -> None:
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", version_range=">=0.90.0")],
        )
        present = _mod(mod_identifier="fabric-api", version_number="0.92.0")
        # Library has a newer one, but the dep is already satisfied in the set.
        newer = _mod(mod_identifier="fabric-api", version_number="0.95.0")
        plan = resolve_dependencies(
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer, present],
            library=[newer],
        )
        statuses = {e.dep_identifier: e.status for e in plan.entries}
        assert statuses["fabric-api"] == "already_satisfied"

    def test_present_out_of_range_resolves_as_replacement(self) -> None:
        # X is assigned at v1.0.0 (out of range), the dep needs >=2.0.0, and the
        # library has X v2.0.0. The pick is resolvable_from_library but flags the
        # stale assigned X in ``replaces`` so apply swaps rather than duplicates.
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", version_range=">=2.0.0")],
        )
        stale = _mod(mod_identifier="fabric-api", version_number="1.0.0")
        in_range = _mod(mod_identifier="fabric-api", version_number="2.0.0")
        plan = resolve_dependencies(
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer, stale],
            library=[in_range],
        )
        entry = next(e for e in plan.entries if e.dep_identifier == "fabric-api")
        assert entry.status == "resolvable_from_library"
        assert entry.mod is not None
        assert entry.mod.id == in_range.id
        assert [m.id for m in entry.replaces] == [stale.id]

    def test_absent_dep_has_no_replaces(self) -> None:
        # An absent dep is a plain add: ``replaces`` stays empty.
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", version_range=">=0.90.0")],
        )
        fabric_api = _mod(mod_identifier="fabric-api", version_number="0.92.0")
        plan = resolve_dependencies(
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
            library=[fabric_api],
        )
        assert plan.entries[0].status == "resolvable_from_library"
        assert plan.entries[0].replaces == []

    def test_optional_dep_is_not_resolved(self) -> None:
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", required=False)],
        )
        fabric_api = _mod(mod_identifier="fabric-api")
        plan = resolve_dependencies(
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
            library=[fabric_api],
        )
        assert plan.entries == []

    def test_validation_block_echoed(self) -> None:
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api")],
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.21", assigned=[consumer], library=[]
        )
        assert [m.depends_on for m in plan.validation.missing_deps] == ["fabric-api"]


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


def _seed_assigned(
    uow: FakeUnitOfWork, store: FakeModStore, server: Server, mod: Mod
) -> None:
    from mc_server_dashboard_api.servers.domain.server_mod import (
        ServerModAssignment,
        ServerModId,
    )

    uow.mods.mods[mod.id] = mod
    store.blobs[mod.id] = b"JAR!"
    uow.mods.assignments[ServerModId.new()] = ServerModAssignment(
        id=ServerModId.new(),
        server_id=server.id,
        mod_id=mod.id,
        enabled=True,
        assigned_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _seed_library(uow: FakeUnitOfWork, store: FakeModStore, mod: Mod) -> None:
    uow.mods.mods[mod.id] = mod
    store.blobs[mod.id] = b"JAR!"


def _ctx(
    server: Server,
) -> tuple[FakeUnitOfWork, FakeFileStore, FakeModStore]:
    servers = FakeServerRepository()
    servers.seed(server)
    return FakeUnitOfWork(servers=servers), FakeFileStore(), FakeModStore()


class TestResolveServerMods:
    async def test_plan_classifies_assigned_deps(self) -> None:
        server = _server()
        uow, _file_store, store = _ctx(server)
        consumer = _mod(mod_identifier="sodium", dependencies=[_dep("fabric-api")])
        _seed_assigned(uow, store, server, consumer)
        _seed_library(uow, store, _mod(mod_identifier="fabric-api"))

        plan = await ResolveServerMods(uow, FakeCatalogProvider())(
            community_id=_COMMUNITY_ID, server_id=server.id
        )
        assert len(plan.entries) == 1
        assert plan.entries[0].status == "resolvable_from_library"

    async def test_plan_server_not_found(self) -> None:
        uow, _file_store, _store = _ctx(_server())
        with pytest.raises(ServerNotFoundError):
            await ResolveServerMods(uow, FakeCatalogProvider())(
                community_id=_COMMUNITY_ID, server_id=ServerId(uuid.uuid4())
            )


class TestApplyServerModResolution:
    def _apply(
        self,
        uow: FakeUnitOfWork,
        file_store: FakeFileStore,
        store: FakeModStore,
        provider: FakeCatalogProvider | None = None,
    ) -> ApplyServerModResolution:
        lock = FakeLifecycleLock()
        catalog = provider or FakeCatalogProvider()
        assign = AssignMods(
            uow=uow,
            file_store=file_store,
            store=store,
            clock=FakeClock(_NOW),
            lifecycle_lock=lock,
        )
        unassign = UnassignMod(
            uow=uow,
            file_store=file_store,
            store=store,
            lifecycle_lock=lock,
        )
        import_mod = ImportMod(
            uow=uow,
            store=store,
            clock=FakeClock(_NOW),
            catalog=catalog,
        )
        return ApplyServerModResolution(
            uow=uow,
            assign_mods=assign,
            unassign_mod=unassign,
            import_mod=import_mod,
            catalog=catalog,
        )

    async def test_apply_assigns_resolvable_and_validates_clean(self) -> None:
        server = _server()
        uow, file_store, store = _ctx(server)
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", version_range=">=0.90.0")],
        )
        _seed_assigned(uow, store, server, consumer)
        fabric_api = _mod(mod_identifier="fabric-api", version_number="0.92.0")
        _seed_library(uow, store, fabric_api)

        plan, applied, _failed = await self._apply(uow, file_store, store)(
            community_id=_COMMUNITY_ID, server_id=server.id, applied_by=uuid.uuid4()
        )

        assert applied == [fabric_api.id]
        # The mod was assigned to the server.
        assert any(a.mod_id == fabric_api.id for a in uow.mods.assignments.values())
        # The re-planned result shows the dep satisfied, with no missing finding.
        assert plan.entries[0].status == "already_satisfied"
        assert plan.validation.missing_deps == []

    async def test_apply_replaces_out_of_range_version(self) -> None:
        # Server has X v1.0.0 assigned (out of range); dep requires X >=2.0.0;
        # library has X v2.0.0. Apply must unassign v1.0.0 and assign v2.0.0 so
        # exactly one X remains (in range), the server validates clean, and the
        # re-plan is idempotent (already_satisfied, no duplicate identifier).
        server = _server()
        uow, file_store, store = _ctx(server)
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", version_range=">=2.0.0")],
        )
        stale = _mod(mod_identifier="fabric-api", version_number="1.0.0")
        _seed_assigned(uow, store, server, consumer)
        _seed_assigned(uow, store, server, stale)
        in_range = _mod(mod_identifier="fabric-api", version_number="2.0.0")
        _seed_library(uow, store, in_range)

        plan, applied, _failed = await self._apply(uow, file_store, store)(
            community_id=_COMMUNITY_ID, server_id=server.id, applied_by=uuid.uuid4()
        )

        assert applied == [in_range.id]
        # Exactly one fabric-api assignment remains, and it is the in-range one.
        fabric_api_mod_ids = [
            a.mod_id
            for a in uow.mods.assignments.values()
            if a.server_id == server.id
            and uow.mods.mods[a.mod_id].mod_identifier == "fabric-api"
        ]
        assert fabric_api_mod_ids == [in_range.id]
        assert stale.id not in {a.mod_id for a in uow.mods.assignments.values()}
        # The dep converges: re-plan is already_satisfied and validation is clean.
        entry = next(e for e in plan.entries if e.dep_identifier == "fabric-api")
        assert entry.status == "already_satisfied"
        assert plan.validation.version_unsatisfied == []
        assert plan.validation.missing_deps == []

    async def test_apply_replacement_is_idempotent(self) -> None:
        # A second apply after a replacement re-finds the dep already_satisfied
        # and changes nothing (no further swap).
        server = _server()
        uow, file_store, store = _ctx(server)
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", version_range=">=2.0.0")],
        )
        stale = _mod(mod_identifier="fabric-api", version_number="1.0.0")
        _seed_assigned(uow, store, server, consumer)
        _seed_assigned(uow, store, server, stale)
        in_range = _mod(mod_identifier="fabric-api", version_number="2.0.0")
        _seed_library(uow, store, in_range)

        apply = self._apply(uow, file_store, store)
        await apply(
            community_id=_COMMUNITY_ID, server_id=server.id, applied_by=uuid.uuid4()
        )
        plan, applied, _failed = await apply(
            community_id=_COMMUNITY_ID, server_id=server.id, applied_by=uuid.uuid4()
        )
        assert applied == []
        entry = next(e for e in plan.entries if e.dep_identifier == "fabric-api")
        assert entry.status == "already_satisfied"

    async def test_apply_is_idempotent(self) -> None:
        server = _server()
        uow, file_store, store = _ctx(server)
        consumer = _mod(mod_identifier="sodium", dependencies=[_dep("fabric-api")])
        _seed_assigned(uow, store, server, consumer)
        _seed_library(uow, store, _mod(mod_identifier="fabric-api"))

        apply = self._apply(uow, file_store, store)
        await apply(
            community_id=_COMMUNITY_ID, server_id=server.id, applied_by=uuid.uuid4()
        )
        plan, applied, _failed = await apply(
            community_id=_COMMUNITY_ID, server_id=server.id, applied_by=uuid.uuid4()
        )
        assert applied == []
        assert plan.entries[0].status == "already_satisfied"

    async def test_apply_at_rest_gate_blocks_running_server(self) -> None:
        server = _server(running=True)
        uow, file_store, store = _ctx(server)
        consumer = _mod(mod_identifier="sodium", dependencies=[_dep("fabric-api")])
        _seed_assigned(uow, store, server, consumer)
        _seed_library(uow, store, _mod(mod_identifier="fabric-api"))

        with pytest.raises(ServerFilesUnsettledError):
            await self._apply(uow, file_store, store)(
                community_id=_COMMUNITY_ID, server_id=server.id, applied_by=uuid.uuid4()
            )

    async def test_apply_nothing_resolvable_is_noop(self) -> None:
        server = _server()
        uow, file_store, store = _ctx(server)
        consumer = _mod(mod_identifier="sodium", dependencies=[_dep("fabric-api")])
        _seed_assigned(uow, store, server, consumer)
        # Library has nothing that provides the dep.
        _seed_library(uow, store, _mod(mod_identifier="lithium"))

        plan, applied, _failed = await self._apply(uow, file_store, store)(
            community_id=_COMMUNITY_ID, server_id=server.id, applied_by=uuid.uuid4()
        )
        assert applied == []
        assert plan.entries[0].status == "unresolvable"


# ---------------------------------------------------------------------------
# Modrinth catalog enrichment + auto-import (#1295)
# ---------------------------------------------------------------------------


def _catalog_version(
    *,
    version_id: str = "VER1",
    project_id: str = "FABRICAPI",
    version_number: str = "0.92.0",
    download_url: str,
    loaders: list[str] | None = None,
    game_versions: list[str] | None = None,
    filename: str = "fabric-api.jar",
) -> CatalogVersion:
    return CatalogVersion(
        version_id=version_id,
        project_id=project_id,
        name=f"Fabric API {version_number}",
        version_number=version_number,
        filename=filename,
        download_url=download_url,
        sha512=None,
        loaders=loaders if loaders is not None else ["fabric"],
        game_versions=game_versions if game_versions is not None else ["1.21"],
        dependencies=[],
    )


def _catalog_project(
    *,
    project_id: str = "FABRICAPI",
    slug: str = "fabric-api",
    versions: list[CatalogVersion],
) -> CatalogProject:
    return CatalogProject(
        project_id=project_id,
        slug=slug,
        title="Fabric API",
        description="Core library",
        project_type="mod",
        side="both",
        loaders=["fabric"],
        game_versions=["1.21"],
        versions=versions,
    )


def _search_hit(
    *, project_id: str = "FABRICAPI", slug: str = "fabric-api"
) -> CatalogSearchHit:
    return CatalogSearchHit(
        project_id=project_id,
        slug=slug,
        title="Fabric API",
        description="Core library",
        project_type="mod",
        side="both",
        loaders=["fabric"],
        game_versions=["1.21"],
        downloads=100,
    )


def _make_jar(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _fabric_api_jar(*, version: str = "0.92.0") -> bytes:
    return _make_jar(
        {
            "fabric.mod.json": json.dumps(
                {"id": "fabric-api", "version": version, "depends": {"minecraft": "*"}}
            )
        }
    )


def _fabric_jar(*, mod_id: str, version: str = "0.92.0") -> bytes:
    """A fabric jar whose manifest declares ``mod_id`` (for identity-mismatch)."""
    return _make_jar(
        {
            "fabric.mod.json": json.dumps(
                {"id": mod_id, "version": version, "depends": {"minecraft": "*"}}
            )
        }
    )


class TestSelectImportVersion:
    """Version selection against MC / loader / required-range (#1295)."""

    def test_picks_newest_compatible_version(self) -> None:

        project = _catalog_project(
            versions=[
                _catalog_version(
                    version_id="OLD", version_number="0.91.0", download_url="u1"
                ),
                _catalog_version(
                    version_id="NEW", version_number="0.95.0", download_url="u2"
                ),
            ]
        )
        chosen = _select_import_version(
            project,
            server_type="fabric",
            mc_version="1.21",
            version_range=">=0.90.0",
            range_loader="fabric",
        )
        assert chosen is not None
        assert chosen.version_id == "NEW"

    def test_skips_mc_incompatible_version(self) -> None:

        project = _catalog_project(
            versions=[
                _catalog_version(
                    version_number="0.95.0",
                    game_versions=["1.20.4"],
                    download_url="u",
                ),
            ]
        )
        assert (
            _select_import_version(
                project,
                server_type="fabric",
                mc_version="1.21",
                version_range="",
                range_loader="fabric",
            )
            is None
        )

    def test_skips_loader_incompatible_version(self) -> None:

        project = _catalog_project(
            versions=[
                _catalog_version(
                    version_number="0.95.0", loaders=["forge"], download_url="u"
                ),
            ]
        )
        assert (
            _select_import_version(
                project,
                server_type="fabric",
                mc_version="1.21",
                version_range="",
                range_loader="fabric",
            )
            is None
        )

    def test_skips_out_of_range_version(self) -> None:

        project = _catalog_project(
            versions=[
                _catalog_version(version_number="0.80.0", download_url="u"),
            ]
        )
        assert (
            _select_import_version(
                project,
                server_type="fabric",
                mc_version="1.21",
                version_range=">=0.90.0",
                range_loader="fabric",
            )
            is None
        )


class TestResolveImportsFromCatalog:
    """The read-only plan enrichment that sets ``will_import`` (#1295)."""

    async def test_resolves_needs_import_via_search_fallback(self) -> None:
        # Library has fabric-api only at an out-of-range version -> needs_import.
        # No project_id is captured on the dep, so the resolver searches by id.

        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", version_range=">=0.90.0")],
        )
        too_old = _mod(mod_identifier="fabric-api", version_number="0.80.0")
        plan = resolve_dependencies(
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
            library=[too_old],
        )
        assert plan.entries[0].status == "needs_import"

        project = _catalog_project(
            versions=[_catalog_version(version_number="0.95.0", download_url="u")]
        )
        provider = FakeCatalogProvider(
            projects={"FABRICAPI": project},
            results={"fabric-api": CatalogSearchResult(hits=[_search_hit()], total=1)},
        )
        enriched = await resolve_imports_from_catalog(
            plan,
            catalog=provider,
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
        )
        entry = enriched.entries[0]
        assert entry.status == "needs_import"
        assert entry.will_import is not None
        assert entry.will_import.project_id == "FABRICAPI"
        assert entry.will_import.version_id == "VER1"
        assert entry.will_import.version_number == "0.95.0"

    async def test_resolves_absent_dep_via_captured_project_id(self) -> None:
        # The dep is entirely absent from the library (pure layer -> unresolvable),
        # but the dep edge carries a Modrinth project_id, so the resolver fetches
        # the project directly (no search) and resolves a will_import.

        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[
                {
                    "mod_identifier": "fabric-api",
                    "version_range": "",
                    "required": True,
                    "project_id": "FABRICAPI",
                }
            ],
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.21", assigned=[consumer], library=[]
        )
        assert plan.entries[0].status == "unresolvable"

        project = _catalog_project(
            versions=[_catalog_version(version_number="0.95.0", download_url="u")]
        )
        # No search results seeded: resolution must use the captured project_id.
        provider = FakeCatalogProvider(projects={"FABRICAPI": project})
        enriched = await resolve_imports_from_catalog(
            plan,
            catalog=provider,
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
        )
        entry = enriched.entries[0]
        assert entry.status == "needs_import"
        assert entry.will_import is not None
        assert entry.will_import.version_id == "VER1"

    async def test_stays_unresolvable_when_only_incompatible_versions(self) -> None:

        consumer = _mod(mod_identifier="sodium", dependencies=[_dep("fabric-api")])
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.21", assigned=[consumer], library=[]
        )
        # Project exists but every version is for the wrong MC version.
        project = _catalog_project(
            versions=[
                _catalog_version(
                    version_number="0.95.0",
                    game_versions=["1.20.4"],
                    download_url="u",
                )
            ]
        )
        provider = FakeCatalogProvider(
            projects={"FABRICAPI": project},
            results={"fabric-api": CatalogSearchResult(hits=[_search_hit()], total=1)},
        )
        enriched = await resolve_imports_from_catalog(
            plan,
            catalog=provider,
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
        )
        assert enriched.entries[0].status == "unresolvable"
        assert enriched.entries[0].will_import is None

    async def test_per_dep_catalog_failure_is_isolated(self) -> None:
        # Two needs_import deps: the first's search raises, the second resolves.

        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("broken-dep"), _dep("fabric-api")],
        )
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.21", assigned=[consumer], library=[]
        )

        project = _catalog_project(
            versions=[_catalog_version(version_number="0.95.0", download_url="u")]
        )

        class _FlakyProvider(FakeCatalogProvider):
            async def search(
                self,
                *,
                query: str,
                loader: str | None = None,
                game_version: str | None = None,
                limit: int = 20,
                offset: int = 0,
            ) -> CatalogSearchResult:
                if query == "broken-dep":
                    raise CatalogUnavailableError("down")
                return await super().search(
                    query=query, loader=loader, game_version=game_version
                )

        provider = _FlakyProvider(
            projects={"FABRICAPI": project},
            results={"fabric-api": CatalogSearchResult(hits=[_search_hit()], total=1)},
        )
        enriched = await resolve_imports_from_catalog(
            plan,
            catalog=provider,
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
        )
        by_id = {e.dep_identifier: e for e in enriched.entries}
        # The broken dep's lookup failed -> it stays unresolvable, no crash.
        assert by_id["broken-dep"].status == "unresolvable"
        assert by_id["broken-dep"].will_import is None
        # The healthy dep still resolved.
        assert by_id["fabric-api"].status == "needs_import"
        assert by_id["fabric-api"].will_import is not None

    async def test_no_exact_slug_match_stays_unresolvable(self) -> None:
        # The search returns a hit whose slug != the dep id (and no hit matches
        # exactly). Modrinth slugs frequently differ from manifest ids, so the
        # resolver must NOT blindly take the top hit: the dep stays unresolvable
        # and nothing is selected for import.
        consumer = _mod(mod_identifier="sodium", dependencies=[_dep("fabric-api")])
        plan = resolve_dependencies(
            server_type="fabric", mc_version="1.21", assigned=[consumer], library=[]
        )

        # The (only) hit's slug is an unrelated project; the dep id does not match.
        project = _catalog_project(
            project_id="UNRELATED",
            slug="some-other-mod",
            versions=[_catalog_version(version_number="0.95.0", download_url="u")],
        )
        provider = FakeCatalogProvider(
            projects={"UNRELATED": project},
            results={
                "fabric-api": CatalogSearchResult(
                    hits=[_search_hit(project_id="UNRELATED", slug="some-other-mod")],
                    total=1,
                )
            },
        )
        enriched = await resolve_imports_from_catalog(
            plan,
            catalog=provider,
            server_type="fabric",
            mc_version="1.21",
            assigned=[consumer],
        )
        assert enriched.entries[0].status == "unresolvable"
        assert enriched.entries[0].will_import is None

    async def test_forge_server_resolves_neoforge_only_project(self) -> None:
        # A forge server's search must not be over-constrained to the "forge"
        # loader facet: a NeoForge-only project the server can run (forge/neoforge
        # are loader-compatible) must still surface and resolve.
        consumer = _mod(
            mod_identifier="sodium",
            loader_type="forge",
            dependencies=[_dep("some-lib")],
        )
        plan = resolve_dependencies(
            server_type="forge", mc_version="1.21", assigned=[consumer], library=[]
        )
        assert plan.entries[0].status == "unresolvable"

        project = _catalog_project(
            project_id="SOMELIB",
            slug="some-lib",
            versions=[
                _catalog_version(
                    project_id="SOMELIB",
                    version_number="1.0.0",
                    loaders=["neoforge"],
                    download_url="u",
                )
            ],
        )
        provider = FakeCatalogProvider(
            projects={"SOMELIB": project},
            results={
                "some-lib": CatalogSearchResult(
                    hits=[_search_hit(project_id="SOMELIB", slug="some-lib")], total=1
                )
            },
        )
        enriched = await resolve_imports_from_catalog(
            plan,
            catalog=provider,
            server_type="forge",
            mc_version="1.21",
            assigned=[consumer],
        )
        entry = enriched.entries[0]
        assert entry.status == "needs_import"
        assert entry.will_import is not None
        assert entry.will_import.project_id == "SOMELIB"


class TestApplyImportsFromModrinth:
    """Apply imports the will_import deps into the library and assigns them."""

    def _apply(
        self,
        uow: FakeUnitOfWork,
        file_store: FakeFileStore,
        store: FakeModStore,
        provider: FakeCatalogProvider,
    ) -> ApplyServerModResolution:
        lock = FakeLifecycleLock()
        assign = AssignMods(
            uow=uow,
            file_store=file_store,
            store=store,
            clock=FakeClock(_NOW),
            lifecycle_lock=lock,
        )
        unassign = UnassignMod(
            uow=uow, file_store=file_store, store=store, lifecycle_lock=lock
        )
        import_mod = ImportMod(
            uow=uow, store=store, clock=FakeClock(_NOW), catalog=provider
        )
        return ApplyServerModResolution(
            uow=uow,
            assign_mods=assign,
            unassign_mod=unassign,
            import_mod=import_mod,
            catalog=provider,
        )

    async def test_apply_imports_assigns_and_validates_clean(self) -> None:

        server = _server()
        uow, file_store, store = _ctx(server)
        consumer = _mod(mod_identifier="sodium", dependencies=[_dep("fabric-api")])
        _seed_assigned(uow, store, server, consumer)

        jar = _fabric_api_jar()
        version = _catalog_version(download_url="https://cdn.modrinth.com/fa.jar")
        project = _catalog_project(versions=[version])
        provider = FakeCatalogProvider(
            projects={"FABRICAPI": project},
            results={"fabric-api": CatalogSearchResult(hits=[_search_hit()], total=1)},
            versions={"VER1": version},
            blobs={"https://cdn.modrinth.com/fa.jar": jar},
        )

        plan, applied, failed = await self._apply(uow, file_store, store, provider)(
            community_id=_COMMUNITY_ID, server_id=server.id, applied_by=uuid.uuid4()
        )

        assert failed == []
        # The fabric-api jar was imported into the library and assigned.
        imported = [
            m for m in uow.mods.mods.values() if m.mod_identifier == "fabric-api"
        ]
        assert len(imported) == 1
        assert imported[0].source == "modrinth"
        assert imported[0].id in applied
        assert any(a.mod_id == imported[0].id for a in uow.mods.assignments.values())
        # The dep is now satisfied and the server validates clean.
        entry = next(e for e in plan.entries if e.dep_identifier == "fabric-api")
        assert entry.status == "already_satisfied"
        assert plan.validation.missing_deps == []

    async def test_apply_per_dep_import_failure_isolated(self) -> None:

        server = _server()
        uow, file_store, store = _ctx(server)
        consumer = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("broken-dep"), _dep("fabric-api")],
        )
        _seed_assigned(uow, store, server, consumer)

        jar = _fabric_api_jar()
        fabric_version = _catalog_version(
            download_url="https://cdn.modrinth.com/fa.jar"
        )
        fabric_project = _catalog_project(versions=[fabric_version])
        broken_version = _catalog_version(
            version_id="BROKENVER",
            project_id="BROKEN",
            download_url="https://cdn.modrinth.com/broken.jar",
            filename="broken.jar",
        )
        broken_project = _catalog_project(
            project_id="BROKEN", slug="broken-dep", versions=[broken_version]
        )

        class _FlakyDownload(FakeCatalogProvider):
            async def download(self, url: str) -> bytes:
                if "broken" in url:
                    raise CatalogUnavailableError("cdn down")
                return await super().download(url)

        provider = _FlakyDownload(
            projects={"FABRICAPI": fabric_project, "BROKEN": broken_project},
            results={
                "fabric-api": CatalogSearchResult(hits=[_search_hit()], total=1),
                "broken-dep": CatalogSearchResult(
                    hits=[_search_hit(project_id="BROKEN", slug="broken-dep")], total=1
                ),
            },
            versions={"VER1": fabric_version, "BROKENVER": broken_version},
            blobs={"https://cdn.modrinth.com/fa.jar": jar},
        )

        plan, applied, failed = await self._apply(uow, file_store, store, provider)(
            community_id=_COMMUNITY_ID, server_id=server.id, applied_by=uuid.uuid4()
        )

        # The broken dep's import failed in isolation; fabric-api still applied.
        assert failed == ["broken-dep"]
        imported = [
            m for m in uow.mods.mods.values() if m.mod_identifier == "fabric-api"
        ]
        assert len(imported) == 1
        assert imported[0].id in applied

    async def test_apply_identity_mismatch_not_assigned(self) -> None:
        # An exact-slug project resolves, but the downloaded jar's manifest id is
        # an UNRELATED mod (slug matched, manifest id did not). The post-download
        # identity guard must refuse to assign it and record the dep as a failed
        # import instead of silently assigning the wrong jar.
        server = _server()
        uow, file_store, store = _ctx(server)
        consumer = _mod(mod_identifier="sodium", dependencies=[_dep("fabric-api")])
        _seed_assigned(uow, store, server, consumer)

        # The jar bytes declare "not-fabric-api" — neither the id nor a provides
        # entry matches the dep id "fabric-api".
        jar = _fabric_jar(mod_id="not-fabric-api")
        version = _catalog_version(download_url="https://cdn.modrinth.com/fa.jar")
        project = _catalog_project(versions=[version])
        provider = FakeCatalogProvider(
            projects={"FABRICAPI": project},
            results={"fabric-api": CatalogSearchResult(hits=[_search_hit()], total=1)},
            versions={"VER1": version},
            blobs={"https://cdn.modrinth.com/fa.jar": jar},
        )

        plan, applied, failed = await self._apply(uow, file_store, store, provider)(
            community_id=_COMMUNITY_ID, server_id=server.id, applied_by=uuid.uuid4()
        )

        # The mismatching jar is recorded as a failed import and never assigned.
        assert failed == ["fabric-api"]
        mismatched = [
            m for m in uow.mods.mods.values() if m.mod_identifier == "not-fabric-api"
        ]
        # Even though the jar may persist in the library, it must not be assigned.
        mismatched_ids = {m.id for m in mismatched}
        assert all(
            a.mod_id not in mismatched_ids for a in uow.mods.assignments.values()
        )
        assert all(mid not in applied for mid in mismatched_ids)
        # The dep is still an open finding (nothing satisfied it).
        entry = next(e for e in plan.entries if e.dep_identifier == "fabric-api")
        assert entry.status in ("needs_import", "unresolvable")

    async def test_get_plan_does_not_import(self) -> None:
        # GET (ResolveServerMods) enriches with will_import but never downloads.

        server = _server()
        uow, _file_store, store = _ctx(server)
        consumer = _mod(mod_identifier="sodium", dependencies=[_dep("fabric-api")])
        _seed_assigned(uow, store, server, consumer)

        jar = _fabric_api_jar()
        version = _catalog_version(download_url="https://cdn.modrinth.com/fa.jar")
        project = _catalog_project(versions=[version])
        provider = FakeCatalogProvider(
            projects={"FABRICAPI": project},
            results={"fabric-api": CatalogSearchResult(hits=[_search_hit()], total=1)},
            versions={"VER1": version},
            blobs={"https://cdn.modrinth.com/fa.jar": jar},
        )

        plan = await ResolveServerMods(uow, provider)(
            community_id=_COMMUNITY_ID, server_id=server.id
        )
        entry = plan.entries[0]
        assert entry.status == "needs_import"
        assert entry.will_import is not None
        # No download happened and nothing was persisted to the library.
        assert provider.downloads == []
        assert all(m.mod_identifier != "fabric-api" for m in uow.mods.mods.values())

    async def test_apply_at_rest_gate_aborts_running_server(self) -> None:
        # The at-rest gate (a ServerError subclass) on the import-loop assign must
        # NOT be swallowed as a per-dep failure — it aborts the whole apply so the
        # edge returns 409.
        server = _server(running=True)
        uow, file_store, store = _ctx(server)
        consumer = _mod(mod_identifier="sodium", dependencies=[_dep("fabric-api")])
        _seed_assigned(uow, store, server, consumer)

        jar = _fabric_api_jar()
        version = _catalog_version(download_url="https://cdn.modrinth.com/fa.jar")
        project = _catalog_project(versions=[version])
        provider = FakeCatalogProvider(
            projects={"FABRICAPI": project},
            results={"fabric-api": CatalogSearchResult(hits=[_search_hit()], total=1)},
            versions={"VER1": version},
            blobs={"https://cdn.modrinth.com/fa.jar": jar},
        )

        with pytest.raises(ServerFilesUnsettledError):
            await self._apply(uow, file_store, store, provider)(
                community_id=_COMMUNITY_ID, server_id=server.id, applied_by=uuid.uuid4()
            )
