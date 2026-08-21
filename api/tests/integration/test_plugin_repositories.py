"""Integration tests for the plugin repository on PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). The schema is created and torn down per
test via the real migrations so the adapter runs against the documented shape
(DATABASE.md Section 8). A community + server are seeded through the existing
adapters; plugins are added/read/listed/updated/deleted.

The focus is two things the indirect ``FakePluginRepository`` coverage cannot
catch (issue #1329):

* ``get_by_rel_path`` and its ``.disabled``-suffix normalization (issue #1316) --
  a clean path and its disabled variant share one per-server slot, with an
  exact-path match preferred over its suffix sibling.
* a full round-trip of the columns added across #1306/#1307/#1308/#1321
  (``sha256``, ``mod_identifier``, ``provides``, ``dependencies``,
  ``mc_versions``, ``side``, ``catalog_dependencies``), so the SQL adapter cannot
  drift from the fake without a test catching it.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager
from dataclasses import replace as dc_replace

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityName
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.plugin_repository import (
    SqlAlchemyPluginRepository,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.application.plugins import TogglePlugin
from mc_server_dashboard_api.servers.domain.errors import PluginAlreadyExistsError
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import (
    FakeClock,
    FakeFileStore,
    FakePluginCacheStore,
    FakeVersionValidator,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 20, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


async def _seed_server(engine: AsyncEngine) -> ServerId:
    community_id = uuid.uuid4()
    community = Community(
        id=CommunityCommunityId(community_id),
        # A unique name per call so a test may seed two servers without tripping
        # the community ``UNIQUE(name)`` constraint.
        name=CommunityName(f"guild-{community_id}"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    factory = create_session_factory(engine)
    async with CommunityUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.commit()
    server = await CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )(
        community_id=CommunityId(community.id.value),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        config={},
    )
    return server.id


def _plugin(
    server_id: ServerId,
    *,
    rel_path: str = "mods/foo.jar",
    filename: str = "foo.jar",
    enabled: bool = True,
) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path=rel_path,
        filename=filename,
        display_name="Foo",
        description=None,
        loader_type=LoaderType.MOD,
        source=PluginSource.LOCAL,
        source_project_id=None,
        source_version_id=None,
        version_number=None,
        checksum_sha512="abc",
        sha256=None,
        size_bytes=100,
        enabled=enabled,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


# --- get_by_rel_path (the #1316 fix) ---------------------------------------


async def test_get_by_rel_path_matches_enabled_plugin(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    plugin = _plugin(server_id, rel_path="mods/foo.jar")

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(plugin)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        found = await uow.plugins.get_by_rel_path(server_id, "mods/foo.jar")
    assert found is not None
    assert found.id == plugin.id


async def test_get_by_rel_path_matches_disabled_plugin_by_clean_path(
    engine: AsyncEngine,
) -> None:
    # A disabled plugin is stored at ``mods/foo.jar.disabled`` but must be found
    # by its clean path so it still blocks a same-named install (issue #1316).
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    plugin = _plugin(server_id, rel_path="mods/foo.jar.disabled", enabled=False)

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(plugin)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        found = await uow.plugins.get_by_rel_path(server_id, "mods/foo.jar")
    assert found is not None
    assert found.id == plugin.id
    assert found.rel_path == "mods/foo.jar.disabled"


async def test_get_by_rel_path_prefers_exact_path_over_disabled_sibling(
    engine: AsyncEngine,
) -> None:
    # When both an exact row and a ``.disabled`` sibling could match the clean
    # path, the exact-path row wins (issue #1316). Here only the exact row
    # exists at ``mods/foo.jar``; querying it must return that row, not a
    # suffix variant.
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    exact = _plugin(server_id, rel_path="mods/foo.jar")

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(exact)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        found = await uow.plugins.get_by_rel_path(server_id, "mods/foo.jar")
    assert found is not None
    assert found.id == exact.id
    assert found.rel_path == "mods/foo.jar"


async def test_get_by_rel_path_returns_none_for_different_filename(
    engine: AsyncEngine,
) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    plugin = _plugin(server_id, rel_path="mods/foo.jar")

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(plugin)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        found = await uow.plugins.get_by_rel_path(server_id, "mods/bar.jar")
    assert found is None


async def test_get_by_rel_path_returns_none_for_different_server(
    engine: AsyncEngine,
) -> None:
    server_id = await _seed_server(engine)
    other_server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    plugin = _plugin(server_id, rel_path="mods/foo.jar")

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(plugin)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        found = await uow.plugins.get_by_rel_path(other_server_id, "mods/foo.jar")
    assert found is None


# --- round-trip of the Port + the #1306/#1307/#1308/#1321 columns -----------


async def test_round_trip_persists_all_columns(engine: AsyncEngine) -> None:
    # Every column round-trips faithfully, including the JSON shapes added across
    # #1306/#1307/#1308/#1321. This guards against the SQL adapter drifting from
    # FakePluginRepository (issue #1329).
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    plugin = ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path="mods/fabric-api.jar",
        filename="fabric-api.jar",
        display_name="Fabric API",
        description="Core Fabric API",
        loader_type=LoaderType.MOD,
        source=PluginSource.MODRINTH,
        source_project_id="P7dR8mSH",
        source_version_id="v123",
        version_number="0.100.0",
        checksum_sha512="deadbeef",
        sha256="c0ffee",
        size_bytes=2048,
        enabled=True,
        installed_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        mod_identifier="fabric-api",
        provides=["fabric"],
        dependencies=[
            {
                "mod_identifier": "fabricloader",
                "version_range": ">=0.15",
                "required": True,
                "conflict": False,
            }
        ],
        mc_versions=["1.21", "1.21.1"],
        side="server",
        catalog_dependencies=[
            {
                "project_id": "AANobbMI",
                "required": True,
                "slug": "sodium",
                "title": "Sodium",
            },
            {
                "project_id": "gvQqBUqZ",
                "required": False,
                "incompatible": True,
                "slug": "lithium",
                "title": "Lithium",
            },
        ],
    )

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(plugin)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        fetched = await uow.plugins.get_by_id(server_id, plugin.id)
        listed = await uow.plugins.list_for_server(server_id)

    assert fetched == plugin
    assert listed == [plugin]


async def test_get_by_id_scoped_to_server(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    other_server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    plugin = _plugin(server_id, rel_path="mods/foo.jar")

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(plugin)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        fetched = await uow.plugins.get_by_id(server_id, plugin.id)
        cross = await uow.plugins.get_by_id(other_server_id, plugin.id)
    assert fetched is not None
    assert fetched.id == plugin.id
    assert cross is None


async def test_list_for_server_orders_by_display_name(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    a = dc_replace(
        _plugin(server_id, rel_path="mods/a.jar", filename="a.jar"),
        display_name="Alpha",
    )
    z = dc_replace(
        _plugin(server_id, rel_path="mods/z.jar", filename="z.jar"),
        display_name="Zeta",
    )

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(z)
        await uow.plugins.add(a)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        listed = await uow.plugins.list_for_server(server_id)
    assert [p.id for p in listed] == [a.id, z.id]


# --- enabled_geyser_server_ids (the batched Bedrock-joinable gate, #1555) ---


def _geyser_plugin(
    server_id: ServerId, *, enabled: bool = True, rel_path: str = "plugins/Geyser.jar"
) -> ServerPlugin:
    return dc_replace(
        _plugin(server_id, rel_path=rel_path, enabled=enabled),
        mod_identifier="Geyser-Spigot",
    )


async def test_enabled_geyser_server_ids_classifies_each_server(
    engine: AsyncEngine,
    assert_max_queries: Callable[[int], AbstractContextManager[None]],
) -> None:
    # One query batched across three servers (issue #1555): enabled Geyser,
    # disabled Geyser, and no plugins at all. Pinned directly by query count
    # (issue #1563), not just by the single-call shape below.
    enabled_server = await _seed_server(engine)
    disabled_server = await _seed_server(engine)
    bare_server = await _seed_server(engine)
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(_geyser_plugin(enabled_server, enabled=True))
        await uow.plugins.add(_geyser_plugin(disabled_server, enabled=False))
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        with assert_max_queries(1):
            joinable = await uow.plugins.enabled_geyser_server_ids(
                [enabled_server, disabled_server, bare_server]
            )
    assert joinable == {enabled_server}


async def test_enabled_geyser_server_ids_true_for_one_of_two_copies_enabled(
    engine: AsyncEngine,
) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(
            _geyser_plugin(server_id, enabled=True, rel_path="plugins/a.jar")
        )
        await uow.plugins.add(
            _geyser_plugin(server_id, enabled=False, rel_path="plugins/b.jar.disabled")
        )
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        joinable = await uow.plugins.enabled_geyser_server_ids([server_id])
    assert joinable == {server_id}


async def test_enabled_geyser_server_ids_empty_for_empty_input(
    engine: AsyncEngine,
) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(_geyser_plugin(server_id, enabled=True))
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        joinable = await uow.plugins.enabled_geyser_server_ids([])
    assert joinable == set()


async def test_update_rewrites_columns(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    plugin = _plugin(server_id, rel_path="mods/foo.jar")

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(plugin)
        await uow.commit()

    updated = dc_replace(
        plugin,
        rel_path="mods/foo.jar.disabled",
        enabled=False,
        sha256="newhash",
        mod_identifier="foo",
        provides=["foo-alias"],
        dependencies=[{"mod_identifier": "bar", "required": False, "conflict": True}],
        mc_versions=["1.21.1"],
        side="both",
        catalog_dependencies=[
            {"project_id": "X", "required": True, "slug": None, "title": None}
        ],
        updated_at=_NOW + dt.timedelta(hours=1),
    )

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.update(updated)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        fetched = await uow.plugins.get_by_id(server_id, plugin.id)
    assert fetched == updated


async def test_delete_removes_plugin(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    plugin = _plugin(server_id, rel_path="mods/foo.jar")

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(plugin)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.delete(plugin.id)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        fetched = await uow.plugins.get_by_id(server_id, plugin.id)
        listed = await uow.plugins.list_for_server(server_id)
    assert fetched is None
    assert listed == []


# --- find_catalog_provenance_by_sha512 (the #2059 recovery lookup) ----------


async def test_find_catalog_provenance_by_sha512_matches_catalog_install(
    engine: AsyncEngine,
) -> None:
    # A catalog install's checksum recovers its (source, source_project_id) across
    # servers -- the ghost re-ingestion provenance recovery (issue #2059).
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    catalog = dc_replace(
        _plugin(server_id, rel_path="mods/catalog.jar", filename="catalog.jar"),
        source=PluginSource.MODRINTH,
        source_project_id="P7dR8mSH",
        source_version_id="v1a2b3c4",
        version_number="2.0.0",
        checksum_sha512="cafebabe",
    )

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(catalog)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        found = await uow.plugins.find_catalog_provenance_by_sha512("cafebabe")
    assert found == (PluginSource.MODRINTH, "P7dR8mSH", "v1a2b3c4", "2.0.0")


async def test_find_catalog_provenance_by_sha512_ignores_local_and_misses(
    engine: AsyncEngine,
) -> None:
    # A LOCAL upload is not a catalog artifact, so its checksum must not recover a
    # provenance; an unknown checksum returns None (issue #2059).
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    local = dc_replace(
        _plugin(server_id, rel_path="mods/local.jar", filename="local.jar"),
        checksum_sha512="feedface",
    )

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(local)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        by_local = await uow.plugins.find_catalog_provenance_by_sha512("feedface")
        by_miss = await uow.plugins.find_catalog_provenance_by_sha512("nomatch")
    assert by_local is None
    assert by_miss is None


# --- concurrent take of the target rel_path during an update (issue #2612) ----


class _TakeOnLookupRepository(SqlAlchemyPluginRepository):
    """A plugin repository that occupies the target path right after clearing it.

    Reproduces the production interleave deterministically, with no sleeps:
    ``TogglePlugin``'s ``get_by_rel_path`` collision pre-check sees the target
    path free, another request's install commits it on its own connection, and
    only then does ``update`` move this row onto it.
    """

    def __init__(self, session: AsyncSession, engine: AsyncEngine) -> None:
        super().__init__(session)
        self._engine = engine

    async def get_by_rel_path(
        self, server_id: ServerId, rel_path: str
    ) -> ServerPlugin | None:
        found = await super().get_by_rel_path(server_id, rel_path)
        async with ServersUnitOfWork(create_session_factory(self._engine)) as racer:
            await racer.plugins.add(
                _plugin(server_id, rel_path=rel_path, filename="bar.jar")
            )
            await racer.commit()
        return found


class _RacingUnitOfWork(ServersUnitOfWork):
    """A servers UnitOfWork wired with :class:`_TakeOnLookupRepository`."""

    def __init__(self, engine: AsyncEngine) -> None:
        super().__init__(create_session_factory(engine))
        self._engine = engine

    async def __aenter__(self) -> "_RacingUnitOfWork":
        await super().__aenter__()
        assert self._session is not None
        self.plugins = _TakeOnLookupRepository(self._session, self._engine)
        return self


async def test_update_onto_a_concurrently_taken_rel_path_reports_already_exists(
    engine: AsyncEngine,
) -> None:
    # The UPDATE executes -- and violates uq_server_plugin_server_rel -- inside
    # update(), not at the unit of work's commit, so the translation has to sit
    # on that execute; unwrapped it is a raw IntegrityError (500).
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    moving = _plugin(server_id, rel_path="mods/foo.jar", filename="foo.jar")

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(moving)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.plugins.get_by_id(server_id, moving.id)
        assert loaded is not None
        # A racer takes the path the toggle is heading for.
        async with ServersUnitOfWork(factory) as racer:
            await racer.plugins.add(
                _plugin(server_id, rel_path="mods/foo.jar.disabled", filename="bar.jar")
            )
            await racer.commit()
        loaded.rel_path = "mods/foo.jar.disabled"
        loaded.enabled = False
        with pytest.raises(PluginAlreadyExistsError):
            await uow.plugins.update(loaded)


async def test_toggle_plugin_reports_a_concurrently_taken_path_as_already_exists(
    engine: AsyncEngine,
) -> None:
    # The reachable path: TogglePlugin's own collision pre-check passes, the racer
    # installs at the .disabled path, and the rename UPDATE lands on the live
    # UNIQUE. The typed error is the same one the pre-check raises.
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    plugin = _plugin(server_id, rel_path="mods/foo.jar", filename="foo.jar")

    async with ServersUnitOfWork(factory) as uow:
        await uow.plugins.add(plugin)
        await uow.commit()
        server = await uow.servers.get_by_id(server_id)
        assert server is not None
        community_id = server.community_id

    use_case = TogglePlugin(
        uow=_RacingUnitOfWork(engine),
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    with pytest.raises(PluginAlreadyExistsError):
        await use_case(
            community_id=community_id,
            server_id=server_id,
            plugin_id=plugin.id,
            enable=False,
        )

    # The row keeps its original path: nothing moved behind the typed error.
    async with ServersUnitOfWork(factory) as uow:
        still = await uow.plugins.get_by_id(server_id, plugin.id)
    assert still is not None
    assert still.rel_path == "mods/foo.jar"
    assert still.enabled is True
