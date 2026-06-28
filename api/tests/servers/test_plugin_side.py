"""Use-case tests for plugin side + client modpack (issue #1308).

Covers side auto-detect on install, side-aware working-set materialization
(client jars are tracked + cached but never deployed; a side override
adds/removes the working-set file), the manual side override use case, and the
client modpack list + zip download.
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
from mc_server_dashboard_api.servers.application.client_modpack import (
    DownloadClientModpack,
    ListClientMods,
)
from mc_server_dashboard_api.servers.application.plugins import (
    InstallPlugin,
    SetPluginSide,
    TogglePlugin,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogFile,
    CatalogProject,
    CatalogVersion,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidPluginSideError,
    PluginAlreadyExistsError,
    PluginNotFoundError,
    ServerFilesUnsettledError,
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

_NOW = dt.datetime(2026, 6, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())


def _server(
    *,
    server_type: ServerType = ServerType.FABRIC,
    desired_state: DesiredState = DesiredState.STOPPED,
    observed_state: ObservedState = ObservedState.STOPPED,
) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=_COMMUNITY,
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


def _fabric_jar(mod_id: str, environment: str | None) -> bytes:
    manifest: dict[str, object] = {"id": mod_id, "version": "1.0.0"}
    if environment is not None:
        manifest["environment"] = environment
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("fabric.mod.json", json.dumps(manifest))
    return buf.getvalue()


def _plugin(
    *,
    server_id: ServerId,
    side: str = "both",
    enabled: bool = True,
    rel_path: str = "mods/test.jar",
    filename: str = "test.jar",
    sha256: str | None = "sha256-test",
) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path=rel_path,
        filename=filename,
        display_name="Test",
        description=None,
        loader_type=LoaderType.MOD,
        source=PluginSource.LOCAL,
        source_project_id=None,
        source_version_id=None,
        version_number=None,
        checksum_sha512="abc",
        sha256=sha256,
        size_bytes=100,
        enabled=enabled,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
        side=side,  # type: ignore[arg-type]
    )


# -- Side auto-detect on install --


async def test_install_client_mod_is_not_deployed() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    cache = FakePluginCacheStore()
    uc = InstallPlugin(uow=uow, file_store=fs, cache=cache, clock=FakeClock(_NOW))

    content = _fabric_jar("clientmod", "client")
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="clientmod.jar",
        display_name="Client Mod",
        content=content,
    )

    assert plugin.side == "client"
    # Tracked + cached, but never written to the working set.
    assert plugin.sha256 is not None
    assert plugin.sha256 in cache.blobs
    assert "mods/clientmod.jar" not in fs.files


async def test_install_server_mod_is_deployed() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    uc = InstallPlugin(
        uow=uow, file_store=fs, cache=FakePluginCacheStore(), clock=FakeClock(_NOW)
    )

    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="servermod.jar",
        display_name="Server Mod",
        content=_fabric_jar("servermod", "server"),
    )

    assert plugin.side == "server"
    assert "mods/servermod.jar" in fs.files


async def test_install_both_mod_is_deployed() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    uc = InstallPlugin(
        uow=uow, file_store=fs, cache=FakePluginCacheStore(), clock=FakeClock(_NOW)
    )

    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="both.jar",
        display_name="Both",
        content=_fabric_jar("bothmod", "*"),
    )

    assert plugin.side == "both"
    assert "mods/both.jar" in fs.files


# -- Catalog install side --


def _seed_catalog(
    catalog: FakeCatalogProvider, *, client_side: str, server_side: str
) -> tuple[str, str, bytes]:
    content = b"catalog-jar-bytes"
    sha512 = hashlib.sha512(content).hexdigest()
    project = CatalogProject(
        project_id="proj-1",
        slug="sodium",
        title="Sodium",
        description="rendering",
        body="",
        author="caffeine",
        icon_url=None,
        downloads=1,
        categories=["optimization"],
        game_versions=["1.20.4"],
        loaders=["fabric"],
        client_side=client_side,
        server_side=server_side,
    )
    version = CatalogVersion(
        version_id="ver-1",
        version_number="0.5.0",
        name="Sodium 0.5.0",
        game_versions=["1.20.4"],
        loaders=["fabric"],
        files=[
            CatalogFile(
                url="https://cdn.modrinth.com/data/sodium.jar",
                filename="sodium.jar",
                size=len(content),
                sha512=sha512,
                primary=True,
            )
        ],
        date_published="2024-01-15T12:00:00Z",
    )
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, content)
    return project.project_id, version.version_id, content


async def test_catalog_install_client_side_not_deployed() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    catalog = FakeCatalogProvider()
    project_id, version_id, _ = _seed_catalog(
        catalog, client_side="required", server_side="unsupported"
    )
    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )

    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id=project_id,
        version_id=version_id,
    )

    assert plugin.side == "client"
    assert "mods/sodium.jar" not in fs.files


async def test_catalog_install_both_side_deployed() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    catalog = FakeCatalogProvider()
    project_id, version_id, _ = _seed_catalog(
        catalog, client_side="required", server_side="required"
    )
    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )

    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id=project_id,
        version_id=version_id,
    )

    assert plugin.side == "both"
    assert "mods/sodium.jar" in fs.files


# -- SetPluginSide override --


async def test_set_side_client_to_both_materializes_working_set() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    cache = FakePluginCacheStore()
    content = b"the-jar-bytes"
    sha256 = hashlib.sha256(content).hexdigest()
    cache.blobs[sha256] = content
    plugin = _plugin(
        server_id=server.id,
        side="client",
        rel_path="mods/clientmod.jar",
        filename="clientmod.jar",
        sha256=sha256,
    )
    uow.plugins.seed(plugin)
    fs = FakeFileStore()  # working set empty (client not deployed)

    uc = SetPluginSide(uow=uow, file_store=fs, cache=cache, clock=FakeClock(_NOW))
    updated = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        plugin_id=plugin.id,
        side="both",
    )

    assert updated.side == "both"
    assert fs.files["mods/clientmod.jar"] == content


async def test_set_side_both_to_client_removes_working_set() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    cache = FakePluginCacheStore()
    content = b"the-jar-bytes"
    sha256 = hashlib.sha256(content).hexdigest()
    cache.blobs[sha256] = content
    plugin = _plugin(
        server_id=server.id,
        side="both",
        rel_path="mods/both.jar",
        filename="both.jar",
        sha256=sha256,
    )
    uow.plugins.seed(plugin)
    fs = FakeFileStore()
    fs.files["mods/both.jar"] = content  # deployed

    uc = SetPluginSide(uow=uow, file_store=fs, cache=cache, clock=FakeClock(_NOW))
    updated = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        plugin_id=plugin.id,
        side="client",
    )

    assert updated.side == "client"
    assert "mods/both.jar" not in fs.files


async def test_set_side_server_to_both_keeps_working_set() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    cache = FakePluginCacheStore()
    content = b"the-jar-bytes"
    sha256 = hashlib.sha256(content).hexdigest()
    cache.blobs[sha256] = content
    plugin = _plugin(
        server_id=server.id, side="server", rel_path="mods/s.jar", sha256=sha256
    )
    uow.plugins.seed(plugin)
    fs = FakeFileStore()
    fs.files["mods/s.jar"] = content

    uc = SetPluginSide(uow=uow, file_store=fs, cache=cache, clock=FakeClock(_NOW))
    updated = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        plugin_id=plugin.id,
        side="both",
    )

    assert updated.side == "both"
    assert "mods/s.jar" in fs.files


async def test_set_side_rejects_invalid_value() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    plugin = _plugin(server_id=server.id)
    uow.plugins.seed(plugin)
    uc = SetPluginSide(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    with pytest.raises(InvalidPluginSideError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=plugin.id,
            side="frontend",
        )


async def test_set_side_requires_at_rest() -> None:
    uow = FakeUnitOfWork()
    server = _server(
        desired_state=DesiredState.RUNNING, observed_state=ObservedState.RUNNING
    )
    uow.servers.seed(server)
    plugin = _plugin(server_id=server.id, side="client")
    uow.plugins.seed(plugin)
    uc = SetPluginSide(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    with pytest.raises(ServerFilesUnsettledError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=plugin.id,
            side="both",
        )


async def test_set_side_plugin_not_found() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = SetPluginSide(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    with pytest.raises(PluginNotFoundError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=PluginId.new(),
            side="both",
        )


async def test_set_side_blocks_cross_plugin_collision() -> None:
    # Defense-in-depth for #1316: a side change must not materialize a jar over a
    # path already occupied by a *different* plugin. Construct the two-same-base
    # state (occupant disabled at the .disabled path) and switch the other plugin
    # client -> both while disabled, whose desired path is that same .disabled
    # path. SetPluginSide must reject it instead of overwriting the occupant.
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    cache = FakePluginCacheStore()
    content = b"the-jar-bytes"
    sha256 = hashlib.sha256(content).hexdigest()
    cache.blobs[sha256] = content
    fs = FakeFileStore()

    # Occupant: a disabled server/both jar holding mods/foo.jar.disabled.
    occupant = _plugin(
        server_id=server.id,
        side="both",
        enabled=False,
        rel_path="mods/foo.jar.disabled",
        filename="foo.jar",
        sha256="occupant-sha",
    )
    uow.plugins.seed(occupant)
    fs.files["mods/foo.jar.disabled"] = b"occupant-bytes"

    # Other plugin: a disabled client jar with the same clean base (mods/foo.jar).
    other = _plugin(
        server_id=server.id,
        side="client",
        enabled=False,
        rel_path="mods/foo.jar",
        filename="foo.jar",
        sha256=sha256,
    )
    uow.plugins.seed(other)

    uc = SetPluginSide(uow=uow, file_store=fs, cache=cache, clock=FakeClock(_NOW))
    with pytest.raises(PluginAlreadyExistsError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=other.id,
            side="both",
        )

    # The occupant's working-set file is untouched.
    assert fs.files["mods/foo.jar.disabled"] == b"occupant-bytes"


# -- Toggle interaction with client-only plugins --


async def test_disable_client_plugin_does_not_touch_working_set() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    plugin = _plugin(
        server_id=server.id, side="client", rel_path="mods/c.jar", enabled=True
    )
    uow.plugins.seed(plugin)
    fs = FakeFileStore()  # client plugin has no working-set file

    uc = TogglePlugin(
        uow=uow, file_store=fs, cache=FakePluginCacheStore(), clock=FakeClock(_NOW)
    )
    updated = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        plugin_id=plugin.id,
        enable=False,
    )

    assert updated.enabled is False
    # rel_path unchanged: no .disabled rename for a non-deployed client jar.
    assert updated.rel_path == "mods/c.jar"
    assert "mods/c.jar" not in fs.files
    assert "mods/c.jar.disabled" not in fs.files


# -- Reconcile: cross-axis transitions around the .disabled invariant (#1308) --


async def test_disabled_client_to_both_then_enable_materializes_from_cache() -> None:
    """Regression for the state-machine bug: a client jar disabled (no rename,
    rel_path stays suffix-less) then switched client -> both while disabled
    (materialized at the .disabled path) must, on enable, end up at the clean path
    with the cached bytes -- not take the old rename branch and self-collide."""

    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    cache = FakePluginCacheStore()
    content = b"the-jar-bytes"
    sha256 = hashlib.sha256(content).hexdigest()
    cache.blobs[sha256] = content
    plugin = _plugin(
        server_id=server.id,
        side="client",
        rel_path="mods/c.jar",
        filename="c.jar",
        sha256=sha256,
    )
    uow.plugins.seed(plugin)
    fs = FakeFileStore()  # client plugin has no working-set file

    toggle = TogglePlugin(uow=uow, file_store=fs, cache=cache, clock=FakeClock(_NOW))
    set_side = SetPluginSide(uow=uow, file_store=fs, cache=cache, clock=FakeClock(_NOW))

    # 1. Disable the client mod (no rename; rel_path stays clean).
    await toggle(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id, enable=False
    )
    assert plugin.rel_path == "mods/c.jar"
    assert "mods/c.jar" not in fs.files
    assert "mods/c.jar.disabled" not in fs.files

    # 2. Switch client -> both while disabled: a disabled server/both jar lives
    # at the .disabled path, materialized from the cache.
    await set_side(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id, side="both"
    )
    assert plugin.side == "both"
    assert plugin.enabled is False
    assert plugin.rel_path == "mods/c.jar.disabled"
    assert fs.files["mods/c.jar.disabled"] == content
    assert "mods/c.jar" not in fs.files

    # 3. Enable: this is the formerly-broken path. Must succeed and place the
    # working-set file at the clean path (rename from .disabled).
    enabled = await toggle(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id, enable=True
    )
    assert enabled.enabled is True
    assert enabled.rel_path == "mods/c.jar"
    assert fs.files["mods/c.jar"] == content
    assert "mods/c.jar.disabled" not in fs.files

    # 4. Re-disable -> re-enable still works (idempotent reconcile).
    await toggle(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id, enable=False
    )
    assert plugin.rel_path == "mods/c.jar.disabled"
    assert fs.files["mods/c.jar.disabled"] == content
    assert "mods/c.jar" not in fs.files

    await toggle(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id, enable=True
    )
    assert plugin.rel_path == "mods/c.jar"
    assert fs.files["mods/c.jar"] == content
    assert "mods/c.jar.disabled" not in fs.files


async def test_disabled_both_to_client_removes_file_then_back_then_enable() -> None:
    """Disable a server/both jar (file at .disabled), switch both -> client (file
    removed), switch back to both while disabled (re-materialized at the .disabled
    path), then enable (file lands at the clean path)."""

    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    cache = FakePluginCacheStore()
    content = b"both-jar-bytes"
    sha256 = hashlib.sha256(content).hexdigest()
    cache.blobs[sha256] = content
    plugin = _plugin(
        server_id=server.id,
        side="both",
        rel_path="mods/b.jar",
        filename="b.jar",
        sha256=sha256,
    )
    uow.plugins.seed(plugin)
    fs = FakeFileStore()
    fs.files["mods/b.jar"] = content  # deployed

    toggle = TogglePlugin(uow=uow, file_store=fs, cache=cache, clock=FakeClock(_NOW))
    set_side = SetPluginSide(uow=uow, file_store=fs, cache=cache, clock=FakeClock(_NOW))

    # 1. Disable: file renamed to the .disabled path.
    await toggle(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id, enable=False
    )
    assert plugin.rel_path == "mods/b.jar.disabled"
    assert fs.files["mods/b.jar.disabled"] == content
    assert "mods/b.jar" not in fs.files

    # 2. both -> client while disabled: working-set file removed entirely.
    to_client = await set_side(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id, side="client"
    )
    assert to_client.side == "client"
    assert "mods/b.jar.disabled" not in fs.files
    assert "mods/b.jar" not in fs.files
    # rel_path normalizes to the clean path for a client jar (no .disabled).
    assert to_client.rel_path == "mods/b.jar"

    # 3. client -> both while disabled: re-materialized at the .disabled path.
    to_both = await set_side(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id, side="both"
    )
    assert to_both.side == "both"
    assert to_both.rel_path == "mods/b.jar.disabled"
    assert fs.files["mods/b.jar.disabled"] == content
    assert "mods/b.jar" not in fs.files

    # 4. Enable: the file lands at the clean path (rename from .disabled).
    await toggle(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id, enable=True
    )
    assert plugin.enabled is True
    assert plugin.rel_path == "mods/b.jar"
    assert fs.files["mods/b.jar"] == content
    assert "mods/b.jar.disabled" not in fs.files


# -- Client modpack list + zip --


async def test_list_client_mods_returns_client_and_both_enabled() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    client = _plugin(
        server_id=server.id, side="client", rel_path="mods/c.jar", filename="c.jar"
    )
    both = _plugin(
        server_id=server.id, side="both", rel_path="mods/b.jar", filename="b.jar"
    )
    server_only = _plugin(
        server_id=server.id, side="server", rel_path="mods/s.jar", filename="s.jar"
    )
    disabled_client = _plugin(
        server_id=server.id,
        side="client",
        rel_path="mods/d.jar.disabled",
        filename="d.jar",
        enabled=False,
    )
    for p in (client, both, server_only, disabled_client):
        uow.plugins.seed(p)

    uc = ListClientMods(uow=uow)
    mods = await uc(community_id=_COMMUNITY, server_id=server.id)

    sides = {m.filename for m in mods}
    assert sides == {"c.jar", "b.jar"}


async def test_download_client_modpack_streams_zip_from_cache() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    cache = FakePluginCacheStore()

    c_bytes = b"client-mod-bytes"
    b_bytes = b"both-mod-bytes"
    c_sha = hashlib.sha256(c_bytes).hexdigest()
    b_sha = hashlib.sha256(b_bytes).hexdigest()
    cache.blobs[c_sha] = c_bytes
    cache.blobs[b_sha] = b_bytes

    client = _plugin(
        server_id=server.id,
        side="client",
        rel_path="mods/c.jar",
        filename="c.jar",
        sha256=c_sha,
    )
    both = _plugin(
        server_id=server.id,
        side="both",
        rel_path="mods/b.jar",
        filename="b.jar",
        sha256=b_sha,
    )
    server_only = _plugin(
        server_id=server.id,
        side="server",
        rel_path="mods/s.jar",
        filename="s.jar",
        sha256="server-sha",
    )
    for p in (client, both, server_only):
        uow.plugins.seed(p)

    uc = DownloadClientModpack(uow=uow, cache=cache)
    stream = await uc(community_id=_COMMUNITY, server_id=server.id)
    chunks = [chunk async for chunk in stream]
    archive = b"".join(chunks)

    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = set(zf.namelist())
        assert names == {"mods/c.jar", "mods/b.jar"}
        assert zf.read("mods/c.jar") == c_bytes
        assert zf.read("mods/b.jar") == b_bytes


# -- Paper: plugins are always server-side only (issue #1342) --


async def test_paper_install_forces_side_server() -> None:
    """A plugin installed on a Paper server always gets side='server'."""
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    fs = FakeFileStore()
    cache = FakePluginCacheStore()
    uc = InstallPlugin(uow=uow, file_store=fs, cache=cache, clock=FakeClock(_NOW))

    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="worldguard.jar",
        display_name="WorldGuard",
        content=b"jar-bytes",
    )
    assert plugin.side == "server"


async def test_paper_catalog_install_forces_side_server() -> None:
    """A Modrinth install on a Paper server always gets side='server',
    regardless of the catalog's client_side/server_side hint."""
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)

    # The catalog project declares both sides as required -- normally 'both'.
    project = CatalogProject(
        project_id="proj-wg",
        slug="worldguard",
        title="WorldGuard",
        description="Protection plugin",
        body="",
        author="sk89q",
        icon_url=None,
        downloads=100_000,
        categories=[],
        game_versions=["1.20.4"],
        loaders=["paper"],
        client_side="required",
        server_side="required",
    )
    content = b"wg-jar-bytes"
    sha512 = hashlib.sha512(content).hexdigest()
    version = CatalogVersion(
        version_id="ver-wg",
        version_number="7.0.0",
        name="WorldGuard 7.0.0",
        game_versions=["1.20.4"],
        loaders=["paper"],
        files=[
            CatalogFile(
                url="https://cdn.modrinth.com/data/worldguard.jar",
                filename="worldguard-7.0.jar",
                size=len(content),
                sha512=sha512,
                primary=True,
            ),
        ],
        date_published="2024-01-15T12:00:00Z",
    )
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, content)

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-wg",
        version_id="ver-wg",
    )
    assert plugin.side == "server"


async def test_paper_set_side_client_rejected() -> None:
    """SetPluginSide rejects side='client' on a Paper server."""
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    plugin = _plugin(
        server_id=server.id,
        side="server",
        rel_path="plugins/wg.jar",
        filename="wg.jar",
    )
    uow.plugins.seed(plugin)
    uc = SetPluginSide(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    with pytest.raises(InvalidPluginSideError, match="paper"):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=plugin.id,
            side="client",
        )


async def test_paper_set_side_both_rejected() -> None:
    """SetPluginSide rejects side='both' on a Paper server."""
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    plugin = _plugin(
        server_id=server.id,
        side="server",
        rel_path="plugins/wg.jar",
        filename="wg.jar",
    )
    uow.plugins.seed(plugin)
    uc = SetPluginSide(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    with pytest.raises(InvalidPluginSideError, match="paper"):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=plugin.id,
            side="both",
        )


async def test_paper_set_side_server_noop() -> None:
    """SetPluginSide accepts side='server' on a Paper server (no-op)."""
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    plugin = _plugin(
        server_id=server.id,
        side="server",
        rel_path="plugins/wg.jar",
        filename="wg.jar",
    )
    uow.plugins.seed(plugin)
    uc = SetPluginSide(
        uow=uow,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    result = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        plugin_id=plugin.id,
        side="server",
    )
    assert result.side == "server"


async def test_fabric_install_respects_manifest_side() -> None:
    """Fabric install still uses the manifest side (not forced to 'server')."""
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.FABRIC)
    uow.servers.seed(server)
    fs = FakeFileStore()
    cache = FakePluginCacheStore()
    uc = InstallPlugin(uow=uow, file_store=fs, cache=cache, clock=FakeClock(_NOW))

    content = _fabric_jar("clientmod", "client")
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="clientmod.jar",
        display_name="Client Mod",
        content=content,
    )
    assert plugin.side == "client"
