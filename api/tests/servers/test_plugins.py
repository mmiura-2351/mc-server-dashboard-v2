"""Use-case tests for plugin management (issue #1150)."""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
import uuid
from dataclasses import replace

import pytest

from mc_server_dashboard_api.servers.application import plugins as plugins_module
from mc_server_dashboard_api.servers.application.archive_validation import (
    run_archive_validation,
)
from mc_server_dashboard_api.servers.application.plugin_manifest import ParsedManifest
from mc_server_dashboard_api.servers.application.plugins import (
    GetPlugin,
    InstallPlugin,
    ListPlugins,
    RemovePlugin,
    SetPluginSide,
    TogglePlugin,
    ValidatePluginSet,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidFilePathError,
    LifecycleLockLostError,
    PluginAlreadyExistsError,
    PluginNotFoundError,
    PortRangeExhaustedError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
    UnsupportedPluginServerTypeError,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.executor_starvation import (
    IN_FLIGHT,
    InFlight,
    assert_default_executor_free,
)
from tests.servers.fakes import (
    FakeClock,
    FakeFileStore,
    FakeLifecycleLock,
    FakePluginCacheStore,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())


def _server(
    *,
    community_id: CommunityId = _COMMUNITY,
    server_type: ServerType = ServerType.FABRIC,
    desired_state: DesiredState = DesiredState.STOPPED,
    observed_state: ObservedState = ObservedState.STOPPED,
) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=community_id,
        name=ServerName("test-server"),
        mc_edition="java",
        mc_version="1.20.4",
        server_type=server_type,
        config={},
        desired_state=desired_state,
        observed_state=observed_state,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _plugin(
    *,
    server_id: ServerId,
    enabled: bool = True,
    rel_path: str | None = None,
) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path=rel_path or "mods/test.jar",
        filename="test.jar",
        display_name="Test Plugin",
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


# -- ListPlugins --


async def test_list_plugins_empty() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = ListPlugins(uow=uow)
    plugins = await uc(community_id=_COMMUNITY, server_id=server.id)
    assert plugins == []


async def test_list_plugins_returns_seeded() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    p = _plugin(server_id=server.id)
    uow.plugins.seed(p)
    uc = ListPlugins(uow=uow)
    plugins = await uc(community_id=_COMMUNITY, server_id=server.id)
    assert len(plugins) == 1
    assert plugins[0].id == p.id


async def test_list_plugins_unknown_server() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = ListPlugins(uow=uow)
    with pytest.raises(ServerNotFoundError):
        await uc(community_id=_COMMUNITY, server_id=ServerId.new())


async def test_list_plugins_unsupported_server_type() -> None:
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.VANILLA)
    uow.servers.seed(server)
    uc = ListPlugins(uow=uow)
    with pytest.raises(UnsupportedPluginServerTypeError):
        await uc(community_id=_COMMUNITY, server_id=server.id)


# -- ValidatePluginSet --


async def test_validate_plugin_set_reports_missing_dep() -> None:
    uow = FakeUnitOfWork()
    server = _server()  # mc_version 1.20.4, fabric
    uow.servers.seed(server)
    sodium = _plugin(server_id=server.id)
    sodium.mod_identifier = "sodium"
    sodium.mc_versions = ["1.20.4"]
    sodium.dependencies = [
        {"mod_identifier": "fabric-api", "version_range": "", "required": True}
    ]
    uow.plugins.seed(sodium)

    uc = ValidatePluginSet(uow=uow)
    result = await uc(community_id=_COMMUNITY, server_id=server.id)

    assert len(result.missing_deps) == 1
    assert result.missing_deps[0].depends_on == "fabric-api"


async def test_validate_plugin_set_reports_mc_mismatch() -> None:
    uow = FakeUnitOfWork()
    server = _server()  # mc_version 1.20.4
    uow.servers.seed(server)
    sodium = _plugin(server_id=server.id)
    sodium.mod_identifier = "sodium"
    sodium.mc_versions = ["1.21"]
    uow.plugins.seed(sodium)

    uc = ValidatePluginSet(uow=uow)
    result = await uc(community_id=_COMMUNITY, server_id=server.id)

    assert len(result.mc_mismatch) == 1
    assert result.mc_mismatch[0].server_mc_version == "1.20.4"


async def test_validate_plugin_set_empty_is_valid() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = ValidatePluginSet(uow=uow)
    result = await uc(community_id=_COMMUNITY, server_id=server.id)
    assert result.missing_deps == []
    assert result.mc_mismatch == []
    assert result.conflicts == []
    assert result.version_unsatisfied == []


async def test_validate_plugin_set_unknown_server() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = ValidatePluginSet(uow=uow)
    with pytest.raises(ServerNotFoundError):
        await uc(community_id=_COMMUNITY, server_id=ServerId.new())


async def test_validate_plugin_set_unsupported_server_type() -> None:
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.VANILLA)
    uow.servers.seed(server)
    uc = ValidatePluginSet(uow=uow)
    with pytest.raises(UnsupportedPluginServerTypeError):
        await uc(community_id=_COMMUNITY, server_id=server.id)


# -- GetPlugin --


async def test_get_plugin_happy_path() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    p = _plugin(server_id=server.id)
    uow.plugins.seed(p)
    uc = GetPlugin(uow=uow)
    result = await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id)
    assert result.id == p.id


async def test_get_plugin_not_found() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = GetPlugin(uow=uow)
    with pytest.raises(PluginNotFoundError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=PluginId.new(),
        )


async def test_get_plugin_server_not_found() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = GetPlugin(uow=uow)
    with pytest.raises(ServerNotFoundError):
        await uc(
            community_id=_COMMUNITY,
            server_id=ServerId.new(),
            plugin_id=PluginId.new(),
        )


# -- InstallPlugin --


async def test_install_fabric_mod() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    uc = InstallPlugin(
        uow=uow, file_store=fs, cache=FakePluginCacheStore(), clock=FakeClock(_NOW)
    )
    content = b"fake-jar-bytes"
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="fabric-api.jar",
        display_name="Fabric API",
        content=content,
    )
    assert plugin.filename == "fabric-api.jar"
    assert plugin.rel_path == "mods/fabric-api.jar"
    assert plugin.loader_type is LoaderType.MOD
    assert plugin.source is PluginSource.LOCAL
    assert plugin.enabled is True
    assert plugin.size_bytes == len(content)
    assert plugin.checksum_sha512 is not None
    assert "mods/fabric-api.jar" in fs.files


async def test_install_parses_manifest_metadata() -> None:
    # The jar manifest is parsed at ingest and its dependency metadata stored
    # (issue #1307): the uniform source for local uploads.
    import io
    import json
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "fabric.mod.json",
            json.dumps(
                {
                    "id": "sodium",
                    "version": "0.5.0",
                    "depends": {"minecraft": "1.20.4", "fabric-api": ">=0.90.0"},
                    "provides": ["sodium-extra"],
                }
            ),
        )
    jar = buf.getvalue()

    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = InstallPlugin(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="sodium.jar",
        display_name="Sodium",
        content=jar,
    )
    assert plugin.mod_identifier == "sodium"
    assert plugin.provides == ["sodium-extra"]
    assert plugin.mc_versions == ["1.20.4"]
    deps = {d["mod_identifier"]: d for d in plugin.dependencies}
    assert deps["fabric-api"]["required"] is True
    assert "minecraft" not in deps


async def test_install_unreadable_jar_stores_empty_manifest() -> None:
    # A jar that is not a readable zip must not block the install (the loader is
    # known from the server type); it simply carries no manifest metadata.
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = InstallPlugin(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="weird.jar",
        display_name="Weird",
        content=b"not a zip",
    )
    assert plugin.mod_identifier is None
    assert plugin.dependencies == []


async def test_installs_parsing_together_leave_the_default_executor_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The manifest read walks the jar's whole zip directory, which for a crafted
    # jar near the 512 MiB cap takes seconds. It runs on the archive-validation
    # pool (issue #3912): on the event loop it would stall every other request,
    # and on the default executor a few such installs would take every worker
    # from the fs Storage calls, the sweep and everything else that runs there.
    in_flight = InFlight()

    def parse(content: bytes, *, loader: str) -> ParsedManifest:
        in_flight.block()
        return ParsedManifest.empty()

    monkeypatch.setattr(plugins_module, "parse_manifest_at_ingest", parse)
    stores = [FakeFileStore() for _ in range(IN_FLIGHT)]
    installs = []
    for n, store in enumerate(stores):
        uow = FakeUnitOfWork()
        server = _server()
        uow.servers.seed(server)
        use_case = InstallPlugin(
            uow=uow,
            file_store=store,
            cache=FakePluginCacheStore(),
            clock=FakeClock(_NOW),
        )
        installs.append(
            asyncio.ensure_future(
                use_case(
                    community_id=_COMMUNITY,
                    server_id=server.id,
                    filename="mod.jar",
                    display_name="Mod",
                    content=f"jar {n}".encode(),
                )
            )
        )
    try:
        await assert_default_executor_free(in_flight)
    finally:
        await asyncio.gather(*installs)

    assert [store.files["mods/mod.jar"] for store in stores] == [
        f"jar {n}".encode() for n in range(IN_FLIGHT)
    ]


# -- what an install holds while its manifest waits for the pool (issue #3912) --
#
# A parse can wait for a pool thread as long as the slowest validation ahead of
# it. An install that held its server's lifecycle lock and an open unit of work
# meanwhile would pin two DB connections, and nothing bounds how many installs
# wait, so the parse runs before either is taken.


class _HeldParse:
    """A manifest parse that signals once it runs, then waits to be released."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.running = asyncio.Event()
        self.release = threading.Event()

    def __call__(self, content: bytes, *, loader: str) -> ParsedManifest:
        self._loop.call_soon_threadsafe(self.running.set)
        if not self.release.wait(10):
            raise AssertionError("the parse was never released")
        return ParsedManifest.empty()


class _CountingUnitOfWork(FakeUnitOfWork):
    """A unit of work that counts how many of its blocks are open."""

    def __init__(self) -> None:
        super().__init__()
        self.open = 0

    async def __aenter__(self) -> FakeUnitOfWork:
        self.open += 1
        return await super().__aenter__()

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.open -= 1
        await super().__aexit__(exc_type, exc, tb)


async def test_an_install_holds_no_lock_and_no_unit_of_work_while_it_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse = _HeldParse()
    monkeypatch.setattr(plugins_module, "parse_manifest_at_ingest", parse)
    uow = _CountingUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    lock = FakeLifecycleLock()
    fs = FakeFileStore()
    use_case = InstallPlugin(
        uow=uow,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
        lifecycle_lock=lock,
    )
    install = asyncio.ensure_future(
        use_case(
            community_id=_COMMUNITY,
            server_id=server.id,
            filename="mod.jar",
            display_name="Mod",
            content=b"jar",
        )
    )
    try:
        await asyncio.wait_for(parse.running.wait(), 10)
        assert lock.events == []
        assert uow.open == 0
    finally:
        parse.release.set()
        await install

    assert lock.events == [
        (server.id, "acquire"),
        (server.id, "check"),  # before the row commit (issue #3927)
        (server.id, "release"),
    ]
    assert fs.files["mods/mod.jar"] == b"jar"


async def test_a_server_that_leaves_rest_during_the_parse_is_unsettled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The parse holds no lock, so a start can land while it runs. Every check is
    # made again under the lock, on the server as it is then.
    parse = _HeldParse()
    monkeypatch.setattr(plugins_module, "parse_manifest_at_ingest", parse)
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    cache = FakePluginCacheStore()
    use_case = InstallPlugin(
        uow=uow,
        file_store=fs,
        cache=cache,
        clock=FakeClock(_NOW),
        lifecycle_lock=FakeLifecycleLock(),
    )
    install = asyncio.ensure_future(
        use_case(
            community_id=_COMMUNITY,
            server_id=server.id,
            filename="mod.jar",
            display_name="Mod",
            content=b"jar",
        )
    )
    try:
        await asyncio.wait_for(parse.running.wait(), 10)
        uow.servers.seed(
            replace(
                server,
                desired_state=DesiredState.RUNNING,
                observed_state=ObservedState.RUNNING,
            )
        )
    finally:
        parse.release.set()

    with pytest.raises(ServerFilesUnsettledError):
        await install
    assert fs.files == {}
    assert cache.blobs == {}
    assert uow.plugins.by_id == {}


async def test_a_vanilla_server_is_refused_without_waiting_for_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Vanilla runs no plugins, so the install is refused before its parse would
    # queue: with every pool thread busy, it still answers at once.
    parses: list[str] = []

    def parse(content: bytes, *, loader: str) -> ParsedManifest:
        parses.append(loader)
        return ParsedManifest.empty()

    monkeypatch.setattr(plugins_module, "parse_manifest_at_ingest", parse)
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.VANILLA)
    uow.servers.seed(server)
    use_case = InstallPlugin(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
        lifecycle_lock=FakeLifecycleLock(),
    )
    in_flight = InFlight()
    holders = [
        asyncio.ensure_future(run_archive_validation(in_flight.block))
        for _ in range(IN_FLIGHT)
    ]
    try:
        await asyncio.wait_for(in_flight.all_running.wait(), 10)
        with pytest.raises(UnsupportedPluginServerTypeError):
            await asyncio.wait_for(
                use_case(
                    community_id=_COMMUNITY,
                    server_id=server.id,
                    filename="mod.jar",
                    display_name="Mod",
                    content=b"jar",
                ),
                5,
            )
    finally:
        in_flight.release.set()
        await asyncio.gather(*holders)

    assert parses == []


async def test_install_jar_with_unreadable_manifest_stores_empty_manifest() -> None:
    # A jar that opens as a zip but whose manifest ``zipfile`` cannot read back
    # (here: flagged encrypted) used to fail the install with a ``RuntimeError``
    # (issue #3779). It installs like any other unreadable jar.
    import io
    import json
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("fabric.mod.json", json.dumps({"id": "sodium"}))
    jar = bytearray(buf.getvalue())
    jar[6] |= 0x01  # the local header's encrypted bit
    jar[jar.find(b"PK\x01\x02") + 8] |= 0x01  # the central directory's, likewise
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = InstallPlugin(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="sodium.jar",
        display_name="Sodium",
        content=bytes(jar),
    )
    assert plugin.mod_identifier is None
    assert plugin.dependencies == []


async def test_install_paper_plugin() -> None:
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    uc = InstallPlugin(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="worldguard.jar",
        display_name="WorldGuard",
        content=b"jar",
    )
    assert plugin.rel_path == "plugins/worldguard.jar"
    assert plugin.loader_type is LoaderType.PLUGIN


async def test_install_rejects_non_jar() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = InstallPlugin(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    with pytest.raises(InvalidFilePathError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            filename="bad.txt",
            display_name="Bad",
            content=b"data",
        )


async def test_install_accepts_uppercase_jar_extension() -> None:
    """Case-insensitive .jar check: .JAR and .Jar should be accepted."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = InstallPlugin(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="MyPlugin.JAR",
        display_name="My Plugin",
        content=b"jar-bytes",
    )
    assert plugin.filename == "MyPlugin.JAR"


async def test_install_requires_at_rest() -> None:
    uow = FakeUnitOfWork()
    server = _server(
        desired_state=DesiredState.RUNNING,
        observed_state=ObservedState.RUNNING,
    )
    uow.servers.seed(server)
    uc = InstallPlugin(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    with pytest.raises(ServerFilesUnsettledError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            filename="test.jar",
            display_name="Test",
            content=b"jar",
        )


# -- RemovePlugin --


async def test_remove_plugin_deletes_file_and_record() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/test.jar"] = b"jar"
    p = _plugin(server_id=server.id)
    uow.plugins.seed(p)
    uc = RemovePlugin(uow=uow, file_store=fs, clock=FakeClock(_NOW))
    await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id)
    assert p.id not in uow.plugins.by_id
    assert "mods/test.jar" not in fs.files


async def test_remove_plugin_not_found() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = RemovePlugin(uow=uow, file_store=FakeFileStore(), clock=FakeClock(_NOW))
    with pytest.raises(PluginNotFoundError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=PluginId.new(),
        )


# -- TogglePlugin --


async def test_disable_plugin() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/test.jar"] = b"jar"
    p = _plugin(server_id=server.id, enabled=True, rel_path="mods/test.jar")
    uow.plugins.seed(p)
    uc = TogglePlugin(
        uow=uow, file_store=fs, cache=FakePluginCacheStore(), clock=FakeClock(_NOW)
    )
    result = await uc(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id, enable=False
    )
    assert result.enabled is False
    assert result.rel_path == "mods/test.jar.disabled"
    assert "mods/test.jar" not in fs.files
    assert "mods/test.jar.disabled" in fs.files


async def test_enable_plugin() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/test.jar.disabled"] = b"jar"
    p = _plugin(server_id=server.id, enabled=False, rel_path="mods/test.jar.disabled")
    uow.plugins.seed(p)
    uc = TogglePlugin(
        uow=uow, file_store=fs, cache=FakePluginCacheStore(), clock=FakeClock(_NOW)
    )
    result = await uc(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id, enable=True
    )
    assert result.enabled is True
    assert result.rel_path == "mods/test.jar"


async def test_toggle_noop_if_already_desired_state() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/test.jar"] = b"jar"
    p = _plugin(server_id=server.id, enabled=True, rel_path="mods/test.jar")
    uow.plugins.seed(p)
    uc = TogglePlugin(
        uow=uow, file_store=fs, cache=FakePluginCacheStore(), clock=FakeClock(_NOW)
    )
    result = await uc(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id, enable=True
    )
    assert result.enabled is True
    assert uow.commits == 0


async def test_toggle_falls_back_to_cache_when_file_missing() -> None:
    """Toggle uses rename_file; if the file is externally deleted, it falls back
    to materializing from the content-addressed cache (#1331 defence-in-depth)."""
    import hashlib

    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    # Seed the plugin cache with content, but do NOT seed fs.files (file is missing).
    content = b"jar-bytes"
    sha256 = hashlib.sha256(content).hexdigest()
    cache = FakePluginCacheStore()
    cache.blobs[sha256] = content
    p = _plugin(server_id=server.id, enabled=True, rel_path="mods/test.jar")
    p.sha256 = sha256
    uow.plugins.seed(p)
    uc = TogglePlugin(uow=uow, file_store=fs, cache=cache, clock=FakeClock(_NOW))
    result = await uc(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id, enable=False
    )
    assert result.enabled is False
    assert result.rel_path == "mods/test.jar.disabled"
    # The file was materialized from cache at the disabled path.
    assert fs.files["mods/test.jar.disabled"] == content


# -- Duplicate install (Bug 1) --


async def test_install_duplicate_raises_already_exists() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    uc = InstallPlugin(
        uow=uow, file_store=fs, cache=FakePluginCacheStore(), clock=FakeClock(_NOW)
    )
    await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="fabric-api.jar",
        display_name="Fabric API",
        content=b"jar-bytes",
    )
    with pytest.raises(PluginAlreadyExistsError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            filename="fabric-api.jar",
            display_name="Fabric API v2",
            content=b"other-jar-bytes",
        )


async def test_install_same_name_blocked_when_existing_is_disabled() -> None:
    # Regression for #1316: the per-server filename dedup must normalise the
    # `.disabled` suffix, so a disabled plugin still blocks a same-named install.
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    install = InstallPlugin(
        uow=uow, file_store=fs, cache=FakePluginCacheStore(), clock=FakeClock(_NOW)
    )
    toggle = TogglePlugin(
        uow=uow, file_store=fs, cache=FakePluginCacheStore(), clock=FakeClock(_NOW)
    )

    plugin = await install(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="collide.jar",
        display_name="Collide",
        content=b"jar-bytes",
    )
    # Disable it: rel_path becomes mods/collide.jar.disabled.
    disabled = await toggle(
        community_id=_COMMUNITY,
        server_id=server.id,
        plugin_id=plugin.id,
        enable=False,
    )
    assert disabled.rel_path == "mods/collide.jar.disabled"

    # A second same-named install must now be rejected (was wrongly allowed).
    with pytest.raises(PluginAlreadyExistsError):
        await install(
            community_id=_COMMUNITY,
            server_id=server.id,
            filename="collide.jar",
            display_name="Collide v2",
            content=b"other-jar-bytes",
        )


# -- Toggle collision (Bug 2) --


async def test_enable_collision_raises_already_exists() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    # Plugin A occupies mods/foo.jar.
    plugin_a = _plugin(server_id=server.id, enabled=True, rel_path="mods/foo.jar")
    uow.plugins.seed(plugin_a)
    fs.files["mods/foo.jar"] = b"jar-a"
    # Plugin B is disabled at mods/foo.jar.disabled.
    plugin_b = _plugin(
        server_id=server.id, enabled=False, rel_path="mods/foo.jar.disabled"
    )
    uow.plugins.seed(plugin_b)
    fs.files["mods/foo.jar.disabled"] = b"jar-b"
    uc = TogglePlugin(
        uow=uow, file_store=fs, cache=FakePluginCacheStore(), clock=FakeClock(_NOW)
    )
    with pytest.raises(PluginAlreadyExistsError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=plugin_b.id,
            enable=True,
        )


# -- Remove with missing file (Improvement 3) --


async def test_remove_plugin_succeeds_when_jar_already_gone() -> None:
    """Removing a plugin whose jar was already gone still cleans up the DB record."""

    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    p = _plugin(server_id=server.id)
    uow.plugins.seed(p)
    # Nothing is seeded at the plugin's rel_path, so the fake's delete_file
    # misses with ServerFileNotFoundError, as the real seam does (issue #3029).
    uc = RemovePlugin(uow=uow, file_store=FakeFileStore(), clock=FakeClock(_NOW))
    await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id)
    assert p.id not in uow.plugins.by_id


# -- Content-addressed cache (issue #1306) --


async def test_install_stores_sha256_and_caches_blob() -> None:
    """Install computes the sha256 content address and caches the jar blob."""
    import hashlib

    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    cache = FakePluginCacheStore()
    uc = InstallPlugin(
        uow=uow, file_store=FakeFileStore(), cache=cache, clock=FakeClock(_NOW)
    )
    content = b"fabric-jar-bytes"
    expected_sha256 = hashlib.sha256(content).hexdigest()
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="fabric-api.jar",
        display_name="Fabric API",
        content=content,
    )
    assert plugin.sha256 == expected_sha256
    cached = b"".join([chunk async for chunk in cache.open(expected_sha256)])
    assert cached == content


async def test_install_identical_content_dedups_blob() -> None:
    """A second install of identical bytes (different server) reuses the blob.

    Both rows record the same content address; the cache stores the blob once even
    though ``put`` is called twice (dedup-on-ingest).
    """
    import hashlib

    uow = FakeUnitOfWork()
    server_a = _server()
    server_b = _server()
    uow.servers.seed(server_a)
    uow.servers.seed(server_b)
    cache = FakePluginCacheStore()
    uc = InstallPlugin(
        uow=uow, file_store=FakeFileStore(), cache=cache, clock=FakeClock(_NOW)
    )
    content = b"identical-jar-bytes"
    sha256 = hashlib.sha256(content).hexdigest()

    plugin_a = await uc(
        community_id=_COMMUNITY,
        server_id=server_a.id,
        filename="lib.jar",
        display_name="Lib",
        content=content,
    )
    plugin_b = await uc(
        community_id=_COMMUNITY,
        server_id=server_b.id,
        filename="lib.jar",
        display_name="Lib",
        content=content,
    )

    assert plugin_a.sha256 == sha256
    assert plugin_b.sha256 == sha256
    # put was attempted both times, but only one blob is stored (deduped).
    assert cache.puts == [sha256, sha256]
    assert list(cache.blobs) == [sha256]


# -- Bedrock port on Geyser detection (issue #1541) --

_BEDROCK_RANGE = PortRange(start=19132, end=19141)


def _geyser_jar() -> bytes:
    """A minimal Paper jar whose plugin.yml declares the Geyser-Spigot name."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "plugin.yml",
            "name: Geyser-Spigot\n"
            "version: 2.4.2\n"
            "main: org.geysermc.geyser.platform.spigot.GeyserSpigotPlugin\n",
        )
    return buf.getvalue()


def _geyser_row(*, server_id: ServerId, rel_path: str) -> ServerPlugin:
    """An installed-Geyser plugin row (manifest name recorded at ingest)."""
    p = _plugin(server_id=server_id, rel_path=rel_path)
    p.mod_identifier = "Geyser-Spigot"
    return p


async def _install_geyser(
    uow: FakeUnitOfWork,
    server: Server,
    *,
    port_range: PortRange | None,
    filename: str = "Geyser-Spigot.jar",
    file_store: FakeFileStore | None = None,
) -> ServerPlugin:
    uc = InstallPlugin(
        uow=uow,
        file_store=file_store or FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
        bedrock_port_range=port_range,
    )
    return await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename=filename,
        display_name="Geyser",
        content=_geyser_jar(),
    )


async def test_install_geyser_allocates_bedrock_port() -> None:
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    await _install_geyser(uow, server, port_range=_BEDROCK_RANGE)
    assert uow.servers.by_id[server.id].bedrock_port == 19132


async def test_install_geyser_without_gate_leaves_port_unset() -> None:
    # bedrock_port_range None = the deployment gate is off (relay disabled or
    # no Bedrock capability): a Geyser install must not allocate.
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    await _install_geyser(uow, server, port_range=None)
    assert uow.servers.by_id[server.id].bedrock_port is None


async def test_install_non_geyser_does_not_allocate() -> None:
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    uc = InstallPlugin(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
        bedrock_port_range=_BEDROCK_RANGE,
    )
    await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="worldguard.jar",
        display_name="WorldGuard",
        content=b"jar",
    )
    assert uow.servers.by_id[server.id].bedrock_port is None


async def test_install_geyser_picks_lowest_free_port() -> None:
    uow = FakeUnitOfWork()
    other = _server(server_type=ServerType.PAPER)
    other.bedrock_port = 19132
    uow.servers.seed(other)
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    await _install_geyser(uow, server, port_range=_BEDROCK_RANGE)
    assert uow.servers.by_id[server.id].bedrock_port == 19133


async def test_install_geyser_skips_reserved_tunnel_port() -> None:
    # The relay's Bedrock tunnel UDP port inside the window is never handed out.
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    reserved = PortRange(start=19132, end=19141, reserved=frozenset({19132}))
    await _install_geyser(uow, server, port_range=reserved)
    assert uow.servers.by_id[server.id].bedrock_port == 19133


async def test_install_geyser_keeps_existing_port() -> None:
    # A second Geyser jar on an already Bedrock-enabled server re-allocates
    # nothing: the server keeps its port.
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    server.bedrock_port = 19140
    uow.servers.seed(server)
    await _install_geyser(uow, server, port_range=_BEDROCK_RANGE)
    assert uow.servers.by_id[server.id].bedrock_port == 19140


async def test_install_geyser_exhausted_window_aborts_install() -> None:
    uow = FakeUnitOfWork()
    other = _server(server_type=ServerType.PAPER)
    other.bedrock_port = 19132
    uow.servers.seed(other)
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    fs = FakeFileStore()
    with pytest.raises(PortRangeExhaustedError):
        await _install_geyser(
            uow, server, port_range=PortRange(start=19132, end=19132), file_store=fs
        )
    # Nothing committed (the real UoW rolls the transaction back): the abort
    # signal is zero commits; the fake's staged plugin add is not transactional.
    assert uow.commits == 0
    assert uow.servers.by_id[server.id].bedrock_port is None
    # The file store is outside the SQL transaction: the failed install must not
    # leave an orphaned working-set jar behind (allocation runs before the write).
    assert fs.files == {}


async def test_remove_geyser_releases_bedrock_port() -> None:
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    server.bedrock_port = 19132
    uow.servers.seed(server)
    p = _geyser_row(server_id=server.id, rel_path="plugins/Geyser-Spigot.jar")
    uow.plugins.seed(p)
    uc = RemovePlugin(uow=uow, file_store=FakeFileStore(), clock=FakeClock(_NOW))
    await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id)
    assert uow.servers.by_id[server.id].bedrock_port is None


async def test_remove_non_geyser_keeps_bedrock_port() -> None:
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    server.bedrock_port = 19132
    uow.servers.seed(server)
    p = _plugin(server_id=server.id, rel_path="plugins/worldguard.jar")
    uow.plugins.seed(p)
    uc = RemovePlugin(uow=uow, file_store=FakeFileStore(), clock=FakeClock(_NOW))
    await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id)
    assert uow.servers.by_id[server.id].bedrock_port == 19132


async def test_remove_geyser_keeps_port_while_another_geyser_remains() -> None:
    # Two Geyser jars (e.g. a catalog install plus a local upload): the port is
    # released only when the last one leaves.
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    server.bedrock_port = 19132
    uow.servers.seed(server)
    first = _geyser_row(server_id=server.id, rel_path="plugins/Geyser-Spigot.jar")
    second = _geyser_row(server_id=server.id, rel_path="plugins/geyser-copy.jar")
    uow.plugins.seed(first)
    uow.plugins.seed(second)
    uc = RemovePlugin(uow=uow, file_store=FakeFileStore(), clock=FakeClock(_NOW))
    await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=first.id)
    assert uow.servers.by_id[server.id].bedrock_port == 19132


# -- Commit-before-file-ops ordering (issue #1706) --


class _FailingCommitUoW(FakeUnitOfWork):
    """A UoW whose commit always raises, simulating a DB commit failure."""

    async def commit(self) -> None:
        raise RuntimeError("simulated commit failure")


async def test_install_failed_commit_leaves_no_orphan_jar() -> None:
    """A failed DB commit must not leave a working-set jar behind (#1706)."""
    uow = _FailingCommitUoW()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    uc = InstallPlugin(
        uow=uow, file_store=fs, cache=FakePluginCacheStore(), clock=FakeClock(_NOW)
    )
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            filename="fabric-api.jar",
            display_name="Fabric API",
            content=b"jar-bytes",
        )
    # The jar must NOT have been written to the working set.
    assert fs.files == {}


async def test_remove_failed_commit_leaves_jar_intact() -> None:
    """A failed DB commit must not delete the working-set jar (#1706)."""
    uow = _FailingCommitUoW()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/test.jar"] = b"jar"
    p = _plugin(server_id=server.id)
    uow.plugins.seed(p)
    uc = RemovePlugin(uow=uow, file_store=fs, clock=FakeClock(_NOW))
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id)
    # The jar must still exist on disk.
    assert "mods/test.jar" in fs.files


async def test_toggle_failed_commit_leaves_file_unchanged() -> None:
    """A failed DB commit must not rename the working-set jar (#1706)."""
    uow = _FailingCommitUoW()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/test.jar"] = b"jar"
    p = _plugin(server_id=server.id, enabled=True, rel_path="mods/test.jar")
    uow.plugins.seed(p)
    uc = TogglePlugin(
        uow=uow, file_store=fs, cache=FakePluginCacheStore(), clock=FakeClock(_NOW)
    )
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=p.id,
            enable=False,
        )
    # The file must remain at its original path (not renamed to .disabled).
    assert "mods/test.jar" in fs.files
    assert "mods/test.jar.disabled" not in fs.files


async def test_set_side_failed_commit_leaves_file_unchanged() -> None:
    """A failed DB commit must not materialize/remove the working-set jar (#1706)."""
    import hashlib

    uow = _FailingCommitUoW()
    server = _server()
    uow.servers.seed(server)
    cache = FakePluginCacheStore()
    content = b"the-jar-bytes"
    sha256 = hashlib.sha256(content).hexdigest()
    cache.blobs[sha256] = content
    plugin = _plugin(server_id=server.id)
    plugin.side = "client"
    plugin.sha256 = sha256
    uow.plugins.seed(plugin)
    fs = FakeFileStore()  # working set empty (client not deployed)

    uc = SetPluginSide(uow=uow, file_store=fs, cache=cache, clock=FakeClock(_NOW))
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=plugin.id,
            side="both",
        )
    # The jar must NOT have been materialized into the working set.
    assert fs.files == {}


# -- A lost lifecycle lock (issue #3927) --
#
# Each mutation commits its row and then writes the working set; both rely on
# the lifecycle lock, so a holder that lost it commits and writes nothing.


async def test_install_that_lost_the_lifecycle_lock_commits_and_writes_nothing() -> (
    None
):
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    lock = _lost_lock()

    with pytest.raises(LifecycleLockLostError):
        await InstallPlugin(
            uow=uow,
            file_store=fs,
            cache=FakePluginCacheStore(),
            clock=FakeClock(_NOW),
            lifecycle_lock=lock,
        )(
            community_id=_COMMUNITY,
            server_id=server.id,
            filename="fabric-api.jar",
            display_name="Fabric API",
            content=b"fake-jar-bytes",
        )

    assert uow.commits == 0
    assert fs.files == {}


async def test_remove_that_lost_the_lifecycle_lock_commits_and_deletes_nothing() -> (
    None
):
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/test.jar"] = b"jar"
    p = _plugin(server_id=server.id)
    uow.plugins.seed(p)

    with pytest.raises(LifecycleLockLostError):
        await RemovePlugin(
            uow=uow, file_store=fs, clock=FakeClock(_NOW), lifecycle_lock=_lost_lock()
        )(community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id)

    assert uow.commits == 0
    assert fs.files == {"mods/test.jar": b"jar"}


async def test_toggle_that_lost_the_lifecycle_lock_commits_and_moves_nothing() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/test.jar"] = b"jar"
    p = _plugin(server_id=server.id, enabled=True, rel_path="mods/test.jar")
    uow.plugins.seed(p)

    with pytest.raises(LifecycleLockLostError):
        await TogglePlugin(
            uow=uow,
            file_store=fs,
            cache=FakePluginCacheStore(),
            clock=FakeClock(_NOW),
            lifecycle_lock=_lost_lock(),
        )(community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id, enable=False)

    assert uow.commits == 0
    assert fs.files == {"mods/test.jar": b"jar"}


async def test_set_side_that_lost_the_lifecycle_lock_commits_and_moves_nothing() -> (
    None
):
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/test.jar"] = b"jar"
    p = _plugin(server_id=server.id, enabled=True, rel_path="mods/test.jar")
    uow.plugins.seed(p)

    with pytest.raises(LifecycleLockLostError):
        await SetPluginSide(
            uow=uow,
            file_store=fs,
            cache=FakePluginCacheStore(),
            clock=FakeClock(_NOW),
            lifecycle_lock=_lost_lock(),
        )(community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id, side="client")

    assert uow.commits == 0
    assert fs.files == {"mods/test.jar": b"jar"}


def _lost_lock() -> FakeLifecycleLock:
    lock = FakeLifecycleLock()
    lock.checks_before_loss = 0
    return lock
