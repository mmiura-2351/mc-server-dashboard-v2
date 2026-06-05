"""Use-case tests for whole-server ZIP export / import (issue #274).

Exercises :mod:`servers.application.export_import` against the servers fakes (no
DB, no real Storage), per TESTING.md Section 4. Verifies:

- a round-trip: export server A's working set + metadata, then import it as a
  fresh server B, and the published working set + metadata-driven fields match;
- export is at-rest only (running -> 409 via ServerFilesUnsettledError) and
  ``exported_at`` comes from the Clock seam;
- import validation: missing/wrong-format/malformed metadata -> 422; a
  spigot-typed metadata -> 422 (the SAME create-path validator); the name comes
  from the request (uniqueness 409); the row gets an auto-assigned game port;
- import caps: an oversized body / over-cap extraction -> 413;
- import failure posture: a storage write failure mid-publish -> the seed-failure
  503 posture, with the row already created.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import uuid
import zipfile
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.servers.application.export_import import (
    EXPORT_FORMAT_VERSION,
    EXPORT_METADATA_FILENAME,
    ExportServer,
    ImportServer,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidExportMetadataError,
    ServerFilesUnsettledError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from mc_server_dashboard_api.servers.domain.version_validator import (
    SpigotUnsupportedError,
)
from tests.servers.fakes import (
    FakeClock,
    FakeFileStore,
    FakeUnitOfWork,
    FakeVersionValidator,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_PORT_RANGE = PortRange(start=25565, end=25600)


def _server(*, community_id: uuid.UUID, server_id: uuid.UUID) -> Server:
    return Server(
        id=ServerId(server_id),
        community_id=CommunityId(community_id),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
        game_port=25565,
    )


def _create_server(uow: FakeUnitOfWork, store: FakeFileStore) -> CreateServer:
    return CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=store,
        port_range=_PORT_RANGE,
    )


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _metadata(
    *, server_type: str = "vanilla", mc_version: str = "1.21.1", **overrides: object
) -> bytes:
    body: dict[str, object] = {
        "format": EXPORT_FORMAT_VERSION,
        "name": "exported-name",
        "mc_edition": "java",
        "mc_version": mc_version,
        "server_type": server_type,
        "exported_at": _NOW.isoformat(),
    }
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


async def _drain(stream: AsyncIterator[bytes]) -> bytes:
    out = bytearray()
    async for chunk in stream:
        out += chunk
    return bytes(out)


# --- export ----------------------------------------------------------------


async def test_export_streams_working_set_plus_metadata() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    store = FakeFileStore()
    store.files["server.properties"] = b"motd=hi"
    store.files["world/level.dat"] = b"\x00\x01"
    use_case = ExportServer(uow=uow, clock=FakeClock(_NOW), file_store=store)

    stream = await use_case(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    data = await _drain(stream)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert "server.properties" in names
        assert "world/level.dat" in names
        assert EXPORT_METADATA_FILENAME in names
        meta = json.loads(zf.read(EXPORT_METADATA_FILENAME))
    assert meta["format"] == EXPORT_FORMAT_VERSION
    assert meta["server_type"] == "vanilla"
    assert meta["mc_version"] == "1.21.1"
    assert meta["exported_at"] == _NOW.isoformat()


async def test_export_running_is_unsettled() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    running = _server(community_id=community, server_id=server_id)
    running.desired_state = DesiredState.RUNNING
    running.observed_state = ObservedState.RUNNING
    from mc_server_dashboard_api.servers.domain.value_objects import WorkerId

    running.assigned_worker_id = WorkerId(worker)
    uow.servers.seed(running)
    use_case = ExportServer(uow=uow, clock=FakeClock(_NOW), file_store=FakeFileStore())

    with pytest.raises(ServerFilesUnsettledError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )


# --- round-trip ------------------------------------------------------------


async def test_export_then_import_round_trips_files_and_metadata() -> None:
    community = uuid.uuid4()
    src_id = uuid.uuid4()

    src_uow = FakeUnitOfWork()
    src_uow.servers.seed(_server(community_id=community, server_id=src_id))
    src_store = FakeFileStore()
    src_store.files["server.properties"] = b"motd=hi"
    src_store.files["world/level.dat"] = b"\x00\x01\x02"
    export = ExportServer(uow=src_uow, clock=FakeClock(_NOW), file_store=src_store)
    archive = await _drain(
        await export(community_id=CommunityId(community), server_id=ServerId(src_id))
    )

    dst_uow = FakeUnitOfWork()
    dst_store = FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    server = await imp(
        community_id=CommunityId(community),
        name="imported",
        execution_backend="host_process",
        content=archive,
    )

    # The metadata-driven fields match the source; the name comes from the request.
    assert server.name.value == "imported"
    assert server.mc_version == "1.21.1"
    assert server.server_type is ServerType.VANILLA
    assert server.game_port is not None
    # The working set is published identically, and the metadata file is excluded.
    assert dst_store.files["server.properties"] == b"motd=hi"
    assert dst_store.files["world/level.dat"] == b"\x00\x01\x02"
    assert EXPORT_METADATA_FILENAME not in dst_store.files


# --- import validation -----------------------------------------------------


async def test_import_rejects_non_zip_body() -> None:
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    with pytest.raises(InvalidExportMetadataError):
        await imp(
            community_id=CommunityId(uuid.uuid4()),
            name="x",
            execution_backend="host_process",
            content=b"not a zip",
        )


async def test_import_rejects_missing_metadata() -> None:
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    with pytest.raises(InvalidExportMetadataError):
        await imp(
            community_id=CommunityId(uuid.uuid4()),
            name="x",
            execution_backend="host_process",
            content=_zip({"server.properties": b"motd=hi"}),
        )


async def test_import_rejects_wrong_format_version() -> None:
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    bad = _zip({EXPORT_METADATA_FILENAME: _metadata(format=99)})
    with pytest.raises(InvalidExportMetadataError):
        await imp(
            community_id=CommunityId(uuid.uuid4()),
            name="x",
            execution_backend="host_process",
            content=bad,
        )


async def test_import_rejects_malformed_json() -> None:
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    bad = _zip({EXPORT_METADATA_FILENAME: b"{not json"})
    with pytest.raises(InvalidExportMetadataError):
        await imp(
            community_id=CommunityId(uuid.uuid4()),
            name="x",
            execution_backend="host_process",
            content=bad,
        )


async def test_import_spigot_metadata_is_unsupported() -> None:
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(server_type="spigot")})
    with pytest.raises(SpigotUnsupportedError):
        await imp(
            community_id=CommunityId(uuid.uuid4()),
            name="x",
            execution_backend="host_process",
            content=archive,
        )


async def test_import_assigns_game_port() -> None:
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata()})
    server = await imp(
        community_id=CommunityId(community),
        name="fresh",
        execution_backend="host_process",
        content=archive,
    )
    assert _PORT_RANGE.start <= server.game_port <= _PORT_RANGE.end  # type: ignore[operator]


async def test_import_oversized_is_too_large() -> None:
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    archive = _zip(
        {
            EXPORT_METADATA_FILENAME: _metadata(),
            "world/region.bin": b"x" * 64,
        }
    )
    # Tiny caps trip the cumulative-size guard during extraction with a small body.
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store),
        file_store=dst_store,
        max_bytes=16,
        max_entries=100,
    )
    with pytest.raises(FileTooLargeError):
        await imp(
            community_id=CommunityId(community),
            name="fresh",
            execution_backend="host_process",
            content=archive,
        )


async def test_import_publish_failure_is_seed_failed() -> None:
    community = uuid.uuid4()
    dst_uow = FakeUnitOfWork()

    class _FailWorldStore(FakeFileStore):
        # Let create's own seeding (server.properties / eula.txt) succeed, but fail
        # the import publish of a working-set file, so the failure exercises the
        # import publish posture rather than create's seeding posture.
        async def write_file(self, *, community_id, server_id, rel_path, content):  # type: ignore[no-untyped-def]
            if rel_path.startswith("world/"):
                raise RuntimeError("forced storage write failure")
            await super().write_file(
                community_id=community_id,
                server_id=server_id,
                rel_path=rel_path,
                content=content,
            )

    failing_store = _FailWorldStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, failing_store),
        file_store=failing_store,
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(), "world/level.dat": b"\x00"})
    with pytest.raises(WorkingSetSeedFailedError):
        await imp(
            community_id=CommunityId(community),
            name="fresh",
            execution_backend="host_process",
            content=archive,
        )
    # The row was created before the publish failed (degraded but repairable).
    assert len(dst_uow.servers.by_id) == 1
