"""Use-case tests for whole-server ZIP export / import (issue #274).

Exercises :mod:`servers.application.export_import` against the servers fakes (no
DB, no real Storage), per TESTING.md Section 4. Verifies:

- a round-trip: export server A's working set + metadata, then import it as a
  fresh server B, and the published working set + metadata-driven fields match;
- export is at-rest only (running -> 409 via ServerFilesUnsettledError) and
  ``exported_at`` comes from the Clock seam;
- import validation: missing/wrong-format/malformed metadata -> 422; the name comes
  from the request (uniqueness 409); the row gets an auto-assigned game port;
- import caps: an oversized body / over-cap extraction -> 413;
- import failure posture: a storage write failure mid-publish -> the seed-failure
  503 posture, with the row already created;
- import Bedrock enablement (issue #1551): a re-created Geyser plugin allocates
  the imported server's ``bedrock_port`` when the deployment gate is on, leaves it
  unset when the gate is off, a non-Geyser plugin never allocates, and a Bedrock
  window exhaustion or the UNIQUE(bedrock_port) racer backstop surfaces the same
  seed-failure 503 posture (never an unmapped 500).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import threading
import uuid
import zipfile
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from mc_server_dashboard_api.servers.application import export_import
from mc_server_dashboard_api.servers.application.export_import import (
    EXPORT_FORMAT_VERSION,
    EXPORT_METADATA_FILENAME,
    ExportServer,
    ImportServer,
    ResolveServerExport,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.domain.display_name import checked_display_name
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidExportMetadataError,
    InvalidFilePathError,
    InvalidServerNameError,
    LifecycleLockLostError,
    PortAlreadyTakenError,
    ServerAssignedError,
    ServerFilesUnsettledError,
    UnreadableArchiveError,
    WorkingSetSeedFailedError,
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
    WorkerId,
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
    FakeServerRepository,
    FakeUnitOfWork,
    FakeVersionValidator,
)
from tests.servers.test_files import (
    _compressed_zip,
    _with_a_damaged_member,
    _with_its_last_member_encrypted,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_PORT_RANGE = PortRange(start=25565, end=25600)
_BEDROCK_RANGE = PortRange(start=19132, end=19141)


def _server(*, community_id: uuid.UUID, server_id: uuid.UUID) -> Server:
    return Server(
        id=ServerId(server_id),
        community_id=CommunityId(community_id),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
        game_port=25565,
    )


def _create_server(
    uow: FakeUnitOfWork,
    store: FakeFileStore,
    port_range: PortRange = _PORT_RANGE,
) -> CreateServer:
    return CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=store,
        port_range=port_range,
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


def _parse_properties(content: bytes) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in content.decode().splitlines()
        if "=" in line and not line.startswith("#")
    )


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

    export = await use_case(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    data = await _drain(export.stream)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert "server.properties" in names
        assert "world/level.dat" in names
        assert EXPORT_METADATA_FILENAME in names
        meta = json.loads(zf.read(EXPORT_METADATA_FILENAME))
    assert meta["format"] == EXPORT_FORMAT_VERSION
    assert meta["server_type"] == "vanilla"
    assert meta["mc_version"] == "1.21.1"
    # ``exported_at`` is the canonical RFC 3339 ``Z`` form (#674), not the
    # ``+00:00`` offset that ``datetime.isoformat()`` would emit for UTC.
    assert meta["exported_at"] == "2026-06-04T12:00:00Z"


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


@pytest.mark.parametrize(
    "observed", [ObservedState.STOPPED, ObservedState.CRASHED, ObservedState.UNKNOWN]
)
@pytest.mark.parametrize("door", ["export", "resolve"])
async def test_export_refuses_a_server_a_worker_still_holds(
    door: str, observed: ObservedState
) -> None:
    # A stop holds its Worker until the final snapshot settles (issue #3998): an
    # export of ``current`` in that window would lack the snapshot still in
    # flight. The download grant's pre-flight answers what the export would.
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    held = _server(community_id=community, server_id=server_id)
    held.observed_state = observed
    held.assigned_worker_id = WorkerId(uuid.uuid4())
    uow.servers.seed(held)
    store = FakeFileStore()
    store.files["world/level.dat"] = b"w"

    with pytest.raises(ServerAssignedError):
        if door == "export":
            await ExportServer(uow=uow, clock=FakeClock(_NOW), file_store=store)(
                community_id=CommunityId(community), server_id=ServerId(server_id)
            )
        else:
            await ResolveServerExport(uow=uow)(
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
        (
            await export(
                community_id=CommunityId(community), server_id=ServerId(src_id)
            )
        ).stream
    )

    dst_uow = FakeUnitOfWork()
    dst_store = FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    server = await imp(
        community_id=CommunityId(community),
        name="imported",
        content=archive,
    )

    # The metadata-driven fields match the source; the name comes from the request.
    assert server.name.value == "imported"
    assert server.mc_version == "1.21.1"
    assert server.server_type is ServerType.VANILLA
    assert server.game_port is not None
    # The working set is published identically except that import enforces the RCON
    # keys on server.properties (#335); the metadata file is excluded.
    props = dst_store.files["server.properties"].decode()
    assert "motd=hi" in props
    assert "enable-rcon=true" in props
    assert "rcon.port=25575" in props
    assert dst_store.files["world/level.dat"] == b"\x00\x01\x02"
    assert EXPORT_METADATA_FILENAME not in dst_store.files


# --- import: RCON enforcement (issue #335) ---------------------------------


async def test_import_enforces_rcon_on_disabled_properties() -> None:
    # An archive whose server.properties has RCON off / a stray port gets RCON
    # enabled and the port corrected, with a generated password filled in.
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip(
        {
            EXPORT_METADATA_FILENAME: _metadata(),
            "server.properties": b"enable-rcon=false\nrcon.port=1234\nmotd=hi\n",
        }
    )
    await imp(
        community_id=CommunityId(community),
        name="fresh",
        content=archive,
    )
    props = dict(
        line.split("=", 1)
        for line in dst_store.files["server.properties"].decode().splitlines()
        if "=" in line
    )
    assert props["enable-rcon"] == "true"
    assert props["rcon.port"] == "25575"
    assert props["rcon.password"] != ""
    assert props["motd"] == "hi"


async def test_import_preserves_existing_non_empty_rcon_password() -> None:
    # A non-empty existing rcon.password is kept (an importer's known credential),
    # while enable-rcon / rcon.port are still enforced.
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip(
        {
            EXPORT_METADATA_FILENAME: _metadata(),
            "server.properties": (
                b"enable-rcon=false\nrcon.password=known-secret\nrcon.port=1234\n"
            ),
        }
    )
    await imp(
        community_id=CommunityId(community),
        name="fresh",
        content=archive,
    )
    props = dict(
        line.split("=", 1)
        for line in dst_store.files["server.properties"].decode().splitlines()
        if "=" in line
    )
    assert props["enable-rcon"] == "true"
    assert props["rcon.port"] == "25575"
    assert props["rcon.password"] == "known-secret"


async def test_import_seeds_rcon_when_archive_has_no_properties() -> None:
    # An archive with no server.properties at all still ends up with one carrying
    # the enforced RCON keys, so the imported server's console works out of the box.
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(), "world/level.dat": b"\x00"})
    server = await imp(
        community_id=CommunityId(community),
        name="fresh",
        content=archive,
    )
    props = dict(
        line.split("=", 1)
        for line in dst_store.files["server.properties"].decode().splitlines()
        if "=" in line
    )
    assert props["server-port"] == str(server.game_port)
    assert props["enable-rcon"] == "true"
    assert props["rcon.port"] == "25575"
    assert props["rcon.password"] != ""


@pytest.mark.parametrize(
    "members",
    [
        pytest.param({"server.properties": b"motd=hi\n"}, id="archive-properties"),
        pytest.param({"world/level.dat": b"\x00"}, id="no-properties"),
    ],
)
async def test_import_does_not_add_the_new_server_sync_chunk_writes(
    members: dict[str, bytes],
) -> None:
    # sync-chunk-writes=false is what a NEW server starts with (issue #3443). An
    # imported server keeps the settings its archive carries, so the key is not
    # added -- not to the archive's file, and not to the platform keys seeded
    # when the archive has no file.
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(), **members})
    await imp(community_id=CommunityId(uuid.uuid4()), name="fresh", content=archive)
    assert "sync-chunk-writes" not in _parse_properties(
        dst_store.files["server.properties"]
    )


# --- import: platform-managed keys (issue #2621) ----------------------------


async def test_import_republishes_the_db_game_port_over_the_archives() -> None:
    # An archive carrying server-port=25565 imported against a server whose
    # auto-assigned game_port is 26590 must publish the DB's port: nothing else
    # re-applies it after the archive's file lands, and hydrate then copies the
    # archive's value forever (issue #2621).
    community = uuid.uuid4()
    src_id = uuid.uuid4()
    src_uow = FakeUnitOfWork()
    src_uow.servers.seed(_server(community_id=community, server_id=src_id))
    src_store = FakeFileStore()
    src_store.files["server.properties"] = b"server-port=25565\nmotd=hi\n"
    export = ExportServer(uow=src_uow, clock=FakeClock(_NOW), file_store=src_store)
    archive = await _drain(
        (
            await export(
                community_id=CommunityId(community), server_id=ServerId(src_id)
            )
        ).stream
    )

    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(
            dst_uow, dst_store, port_range=PortRange(start=26590, end=26600)
        ),
        file_store=dst_store,
    )
    server = await imp(
        community_id=CommunityId(community), name="imported", content=archive
    )

    assert server.game_port == 26590
    props = _parse_properties(dst_store.files["server.properties"])
    assert props["server-port"] == "26590"
    # Everything the platform does NOT own survives verbatim.
    assert props["motd"] == "hi"


async def test_import_clears_the_archives_resource_pack_keys() -> None:
    # The imported server carries no resource-pack assignment, so the DB-owned
    # value of every resource-pack key is "unset": the archive's pointer at the
    # source deployment's pack must not survive into the new server's file
    # (issue #2621).
    community = uuid.uuid4()
    src_id = uuid.uuid4()
    src_uow = FakeUnitOfWork()
    src_uow.servers.seed(_server(community_id=community, server_id=src_id))
    src_store = FakeFileStore()
    src_store.files["server.properties"] = (
        b"resource-pack=https://source/pack.zip\n"
        b"resource-pack-sha1=0123456789abcdef\n"
        b"require-resource-pack=true\n"
        b"resource-pack-prompt=install it\n"
        b"motd=hi\n"
    )
    export = ExportServer(uow=src_uow, clock=FakeClock(_NOW), file_store=src_store)
    archive = await _drain(
        (
            await export(
                community_id=CommunityId(community), server_id=ServerId(src_id)
            )
        ).stream
    )

    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    await imp(community_id=CommunityId(community), name="imported", content=archive)

    props = _parse_properties(dst_store.files["server.properties"])
    assert "resource-pack" not in props
    assert "resource-pack-sha1" not in props
    assert "require-resource-pack" not in props
    assert "resource-pack-prompt" not in props
    assert props["motd"] == "hi"


# --- import validation -----------------------------------------------------


# Exports whose bytes do not read back as a zip (issue #3769). A body that is not
# a zip at all fails as the metadata is read; a damaged or encrypted metadata
# member fails there too, and a damaged working-set member fails in the
# validate-first pass over the whole archive. Each is refused before the row is
# created.
_UNREADABLE_IMPORTS = [
    pytest.param(b"not a zip", id="not-a-zip"),
    pytest.param(
        _with_a_damaged_member(
            _compressed_zip(
                {EXPORT_METADATA_FILENAME: _metadata(), "world/level.dat": b"L" * 64}
            ),
            "world/level.dat",
        ),
        id="damaged-member",
    ),
    pytest.param(
        _with_a_damaged_member(
            _compressed_zip({EXPORT_METADATA_FILENAME: _metadata()}),
            EXPORT_METADATA_FILENAME,
        ),
        id="damaged-metadata",
    ),
    pytest.param(
        # The metadata is written last: the helper encrypts the last member.
        _with_its_last_member_encrypted(
            _compressed_zip(
                {"world/level.dat": b"L" * 64, EXPORT_METADATA_FILENAME: _metadata()}
            )
        ),
        id="encrypted-metadata",
    ),
]


@pytest.mark.parametrize("content", _UNREADABLE_IMPORTS)
async def test_import_of_an_unreadable_archive_creates_nothing(content: bytes) -> None:
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    with pytest.raises(UnreadableArchiveError):
        await imp(
            community_id=CommunityId(uuid.uuid4()),
            name="x",
            content=content,
        )
    assert len(dst_uow.servers.by_id) == 0
    assert dst_store.files == {}


async def test_import_rejects_missing_metadata() -> None:
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    with pytest.raises(InvalidExportMetadataError):
        await imp(
            community_id=CommunityId(uuid.uuid4()),
            name="x",
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
            content=bad,
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
            content=archive,
        )
    # The validate-first pass (#277) rejects the hostile archive BEFORE the row is
    # created: a 413 leaves no server row behind, unlike the seed-failure posture.
    assert len(dst_uow.servers.by_id) == 0


async def test_import_zip_slip_entry_creates_no_row() -> None:
    # A zip-slip member (422) is a property of the archive and is rejected by the
    # pre-commit validate pass, so no server row is created (#277).
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(), "../escape.txt": b"pwned"})
    with pytest.raises(InvalidFilePathError):
        await imp(
            community_id=CommunityId(community),
            name="fresh",
            content=archive,
        )
    assert len(dst_uow.servers.by_id) == 0
    assert dst_store.files == {}


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
            content=archive,
        )
    # The row was created before the publish failed (degraded but repairable).
    assert len(dst_uow.servers.by_id) == 1


@pytest.mark.parametrize(
    ("reader", "slow_call"),
    [
        pytest.param("_parse_metadata", 1, id="the-metadata-read"),
        pytest.param("_archive_entries", 1, id="the-dry-run"),
        pytest.param("_archive_entries", 2, id="the-pass-that-publishes-the-entries"),
    ],
)
async def test_imports_reading_together_leave_the_default_executor_free(
    monkeypatch: pytest.MonkeyPatch, reader: str, slow_call: int
) -> None:
    # An import reads the archive three times: the metadata, the dry run before
    # the row is created, and the pass that publishes the entries. A crafted
    # archive costs as much in each, so all three run on the archive-validation
    # pool (issue #3912): on the default executor, a few imports reading for
    # minutes would take every worker from the fs Storage calls, the sweep and
    # everything else that runs there.
    in_flight = InFlight()
    real = getattr(export_import, reader)
    calls: dict[bytes, int] = {}
    counting = threading.Lock()

    def read(*args: object, **kwargs: object) -> object:
        content = next(arg for arg in args if isinstance(arg, bytes))
        with counting:
            calls[content] = calls.get(content, 0) + 1
            this_call = calls[content]
        if this_call == slow_call:
            in_flight.block()
        return real(*args, **kwargs)

    monkeypatch.setattr(export_import, reader, read)
    stores = [FakeFileStore() for _ in range(IN_FLIGHT)]
    imports = []
    for n, store in enumerate(stores):
        use_case = ImportServer(
            create_server=_create_server(FakeUnitOfWork(), store), file_store=store
        )
        archive = _zip(
            {EXPORT_METADATA_FILENAME: _metadata(), "a.txt": f"world {n}".encode()}
        )
        imports.append(
            asyncio.ensure_future(
                use_case(
                    community_id=CommunityId(uuid.uuid4()),
                    name=f"imported-{n}",
                    content=archive,
                )
            )
        )
    try:
        await assert_default_executor_free(in_flight)
    finally:
        await asyncio.gather(*imports)

    assert [store.files["a.txt"] for store in stores] == [
        f"world {n}".encode() for n in range(IN_FLIGHT)
    ]


# --- plugin metadata in export/import (issue #1335) --------------------------


def _plugin(
    *, server_id: uuid.UUID, mod_identifier: str = "fabric-api"
) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=ServerId(server_id),
        rel_path=f"mods/{mod_identifier}.jar",
        filename=f"{mod_identifier}.jar",
        display_name=mod_identifier.title(),
        description="A test plugin",
        loader_type=LoaderType.MOD,
        source=PluginSource.MODRINTH,
        source_project_id="proj-1",
        source_version_id="ver-1",
        version_number="1.0.0",
        checksum_sha512="abc123",
        sha256="def456",
        size_bytes=1024,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
        mod_identifier=mod_identifier,
        provides=["fabric-api-base"],
        dependencies=[
            {
                "mod_identifier": "minecraft",
                "version_range": ">=1.21",
                "required": True,
                "conflict": False,
            }
        ],
        mc_versions=["1.21.1"],
        side="both",
        catalog_dependencies=[
            {
                "project_id": "P7dR8mSH",
                "required": True,
                "slug": "fabric-api",
                "title": "Fabric API",
            }
        ],
    )


async def test_export_includes_plugin_metadata() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    plugin = _plugin(server_id=server_id)
    uow.plugins.seed(plugin)
    store = FakeFileStore()
    store.files["mods/fabric-api.jar"] = b"\x00jar"
    use_case = ExportServer(uow=uow, clock=FakeClock(_NOW), file_store=store)

    export = await use_case(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    data = await _drain(export.stream)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        meta = json.loads(zf.read(EXPORT_METADATA_FILENAME))
    assert "plugins" in meta
    assert len(meta["plugins"]) == 1
    p = meta["plugins"][0]
    assert p["mod_identifier"] == "fabric-api"
    assert p["rel_path"] == "mods/fabric-api.jar"
    assert p["loader_type"] == "mod"
    assert p["source"] == "modrinth"
    assert p["side"] == "both"
    assert p["provides"] == ["fabric-api-base"]
    assert p["catalog_dependencies"] == plugin.catalog_dependencies


async def test_export_no_plugins_has_empty_list() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    store = FakeFileStore()
    store.files["server.properties"] = b"motd=hi"
    use_case = ExportServer(uow=uow, clock=FakeClock(_NOW), file_store=store)

    export = await use_case(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    data = await _drain(export.stream)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        meta = json.loads(zf.read(EXPORT_METADATA_FILENAME))
    assert meta["plugins"] == []


async def test_import_recreates_plugin_records_from_metadata() -> None:
    community = uuid.uuid4()
    src_id = uuid.uuid4()

    # Build an export archive from a server that has a plugin.
    src_uow = FakeUnitOfWork()
    src_uow.servers.seed(_server(community_id=community, server_id=src_id))
    plugin = _plugin(server_id=src_id)
    src_uow.plugins.seed(plugin)
    src_store = FakeFileStore()
    src_store.files["mods/fabric-api.jar"] = b"\x00jar"
    export = ExportServer(uow=src_uow, clock=FakeClock(_NOW), file_store=src_store)
    archive = await _drain(
        (
            await export(
                community_id=CommunityId(community), server_id=ServerId(src_id)
            )
        ).stream
    )

    # Import the archive.
    dst_uow = FakeUnitOfWork()
    dst_store = FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    server = await imp(
        community_id=CommunityId(community),
        name="imported",
        content=archive,
    )

    # The imported server should have the plugin record.
    imported_plugins = await dst_uow.plugins.list_for_server(server.id)
    assert len(imported_plugins) == 1
    p = imported_plugins[0]
    assert p.mod_identifier == "fabric-api"
    assert p.rel_path == "mods/fabric-api.jar"
    assert p.display_name == plugin.display_name
    assert p.loader_type is LoaderType.MOD
    assert p.source is PluginSource.MODRINTH
    assert p.source_project_id == "proj-1"
    assert p.provides == ["fabric-api-base"]
    assert p.side == "both"
    assert p.catalog_dependencies == plugin.catalog_dependencies
    assert p.dependencies == plugin.dependencies
    # The id should be fresh (not the source server's id).
    assert p.id != plugin.id
    assert p.server_id == server.id


async def test_import_old_archive_without_plugins_succeeds() -> None:
    # An archive from before #1335 has no "plugins" key; import proceeds
    # without error and has no plugin rows.
    community = uuid.uuid4()
    dst_uow = FakeUnitOfWork()
    dst_store = FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(), "world/level.dat": b"\x00"})
    server = await imp(
        community_id=CommunityId(community),
        name="fresh",
        content=archive,
    )
    imported_plugins = await dst_uow.plugins.list_for_server(server.id)
    assert imported_plugins == []


# --- import: the name is checked first (issue #3988) ------------------------


async def test_import_refuses_the_name_before_reading_the_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def read(*args: object, **kwargs: object) -> object:
        raise AssertionError("the archive was read")

    monkeypatch.setattr(export_import, "run_archive_validation", read)
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    with pytest.raises(InvalidServerNameError):
        await imp(
            community_id=CommunityId(uuid.uuid4()),
            name="a\x00b",
            content=_zip({EXPORT_METADATA_FILENAME: _metadata()}),
        )
    assert len(dst_uow.servers.by_id) == 0


# --- import: an imported plugin's names (issue #3988) ------------------------


def _plugin_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "rel_path": "mods/fabric-api.jar",
        "filename": "fabric-api.jar",
        "display_name": "Fabric API",
    }
    entry.update(overrides)
    return entry


async def _imported_plugin(entry: dict[str, object]) -> ServerPlugin:
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    server = await imp(
        community_id=CommunityId(uuid.uuid4()),
        name="imported",
        content=_zip({EXPORT_METADATA_FILENAME: _metadata(plugins=[entry])}),
    )
    [plugin] = await dst_uow.plugins.list_for_server(server.id)
    return plugin


# The archive's display name is not one the importing user typed, so a name the
# display-name rule refuses is made to fit it rather than refused: each character
# the rule refuses is replaced with ``_``, the result is cut to 200 characters,
# and a blank one gives way to the file's name less its extension.
@pytest.mark.parametrize(
    ("overrides", "stored"),
    [
        pytest.param({"display_name": "Fabric\nAPI"}, "Fabric_API", id="c0"),
        pytest.param({"display_name": "Fabric\x85API"}, "Fabric_API", id="c1"),
        pytest.param(
            {"display_name": "Fabric\ud800API"}, "Fabric_API", id="lone-surrogate"
        ),
        pytest.param({"display_name": "Fabric\u2028API"}, "Fabric_API", id="u+2028"),
        pytest.param({"display_name": "Fabric\u2029API"}, "Fabric_API", id="u+2029"),
        pytest.param({"display_name": "  Fabric API  "}, "Fabric API", id="stripped"),
        pytest.param({"display_name": "x" * 250}, "x" * 200, id="cut-to-200"),
        pytest.param(
            {"display_name": "x" * 199 + " y"}, "x" * 199, id="cut-then-stripped"
        ),
        pytest.param({"display_name": ""}, "fabric-api", id="empty"),
        pytest.param({"display_name": " \t "}, "fabric-api", id="blank"),
        pytest.param(
            {"display_name": "", "filename": "a\x85b.jar"},
            "a_b",
            id="stem-of-the-sanitised-filename",
        ),
        pytest.param(
            {"display_name": "", "filename": "  .jar"}, ".jar", id="blank-stem"
        ),
    ],
)
async def test_import_fits_a_plugin_display_name_to_the_rule(
    overrides: dict[str, object], stored: str
) -> None:
    plugin = await _imported_plugin(_plugin_entry(**overrides))
    assert plugin.display_name == stored
    assert checked_display_name(plugin.display_name) == plugin.display_name


async def test_import_fits_a_missing_plugin_display_name_to_the_rule() -> None:
    entry = _plugin_entry()
    del entry["display_name"]
    plugin = await _imported_plugin(entry)
    assert plugin.display_name == "fabric-api"


# The archive's file name gets the catalog's treatment: the basename, with each
# character the upload rule refuses replaced with ``_``. Its ``rel_path`` names
# the archive member the file is, so it is kept as it is.
@pytest.mark.parametrize(
    ("filename", "stored"),
    [
        pytest.param("a\x85b.jar", "a_b.jar", id="c1"),
        pytest.param("a\ud800b.jar", "a_b.jar", id="lone-surrogate"),
        pytest.param("a\uffffb.jar", "a_b.jar", id="u+ffff"),
        pytest.param("a\u2028b.jar", "a_b.jar", id="u+2028"),
        pytest.param("../../evil.jar", "evil.jar", id="traversal"),
        pytest.param("a\\..\\evil.jar", "evil.jar", id="backslash"),
        # The upload rule's bound, 255 bytes, not the 246 an install keeps.
        pytest.param("a" * 251 + ".jar", "a" * 251 + ".jar", id="255-bytes"),
    ],
)
async def test_import_sanitises_a_plugin_filename(filename: str, stored: str) -> None:
    plugin = await _imported_plugin(_plugin_entry(filename=filename))
    assert plugin.filename == stored
    assert plugin.rel_path == "mods/fabric-api.jar"


# A plugin entry whose file name is still refused once sanitised, or whose
# ``rel_path`` is no safe relative path, refuses the whole import before the row
# is created.
@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(_plugin_entry(filename="a" * 252 + ".jar"), id="256-bytes"),
        pytest.param(_plugin_entry(filename=".."), id="dotdot"),
        pytest.param(_plugin_entry(filename=""), id="empty-filename"),
        pytest.param(_plugin_entry(filename="   "), id="blank-filename"),
        pytest.param(_plugin_entry(filename=5), id="filename-not-a-string"),
        pytest.param(
            {"rel_path": "mods/fabric-api.jar", "display_name": "x"},
            id="no-filename",
        ),
        pytest.param(_plugin_entry(rel_path="../escape.jar"), id="rel-path-dotdot"),
        pytest.param(_plugin_entry(rel_path="/mods/a.jar"), id="rel-path-absolute"),
        pytest.param(_plugin_entry(rel_path="mods/a\nb.jar"), id="rel-path-c0"),
        pytest.param(_plugin_entry(rel_path=["mods"]), id="rel-path-not-a-string"),
        pytest.param(
            {"filename": "fabric-api.jar", "display_name": "x"}, id="no-rel-path"
        ),
        pytest.param("mods/fabric-api.jar", id="not-an-object"),
    ],
)
async def test_import_refuses_an_unsafe_plugin_entry(entry: object) -> None:
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    with pytest.raises(InvalidExportMetadataError):
        await imp(
            community_id=CommunityId(uuid.uuid4()),
            name="imported",
            content=_zip({EXPORT_METADATA_FILENAME: _metadata(plugins=[entry])}),
        )
    assert len(dst_uow.servers.by_id) == 0
    assert dst_store.files == {}


# --- import: Bedrock port allocation on Geyser detection (issue #1551) -------


async def _export_archive(
    *, community: uuid.UUID, mod_identifier: str | None = None
) -> bytes:
    """Build an export archive from a fresh source server.

    ``mod_identifier`` seeds a single plugin row on the source server (e.g.
    ``"Geyser-Spigot"``); ``None`` (the default) exports with no plugins.
    """

    src_id = uuid.uuid4()
    src_uow = FakeUnitOfWork()
    src_uow.servers.seed(_server(community_id=community, server_id=src_id))
    if mod_identifier is not None:
        src_uow.plugins.seed(_plugin(server_id=src_id, mod_identifier=mod_identifier))
    export = ExportServer(
        uow=src_uow, clock=FakeClock(_NOW), file_store=FakeFileStore()
    )
    return await _drain(
        (
            await export(
                community_id=CommunityId(community), server_id=ServerId(src_id)
            )
        ).stream
    )


async def test_import_with_geyser_allocates_bedrock_port() -> None:
    community = uuid.uuid4()
    archive = await _export_archive(community=community, mod_identifier="Geyser-Spigot")

    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store),
        file_store=dst_store,
        bedrock_port_range=_BEDROCK_RANGE,
    )
    server = await imp(
        community_id=CommunityId(community),
        name="imported",
        content=archive,
    )
    assert dst_uow.servers.by_id[server.id].bedrock_port == 19132


async def test_import_without_geyser_does_not_allocate_bedrock_port() -> None:
    community = uuid.uuid4()
    archive = await _export_archive(community=community, mod_identifier="fabric-api")

    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store),
        file_store=dst_store,
        bedrock_port_range=_BEDROCK_RANGE,
    )
    server = await imp(
        community_id=CommunityId(community),
        name="imported",
        content=archive,
    )
    assert dst_uow.servers.by_id[server.id].bedrock_port is None


async def test_import_geyser_without_gate_leaves_port_unset() -> None:
    # bedrock_port_range None = the deployment gate is off (relay disabled or no
    # Bedrock capability): a re-created Geyser plugin must not allocate, but the
    # import itself still succeeds.
    community = uuid.uuid4()
    archive = await _export_archive(community=community, mod_identifier="Geyser-Spigot")

    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    server = await imp(
        community_id=CommunityId(community),
        name="imported",
        content=archive,
    )
    assert dst_uow.servers.by_id[server.id].bedrock_port is None
    imported_plugins = await dst_uow.plugins.list_for_server(server.id)
    assert len(imported_plugins) == 1


async def test_import_geyser_exhausted_bedrock_window_is_seed_failed() -> None:
    # Window exhaustion during the plugin-metadata import is a post-commit
    # failure (the server row and its working set already committed): it reuses
    # the #243/#252 seed-failure posture rather than the install paths' distinct
    # ``bedrock_port_range_exhausted``, since -- unlike an install-time abort --
    # a row already exists here.
    community = uuid.uuid4()
    archive = await _export_archive(community=community, mod_identifier="Geyser-Spigot")

    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    other = _server(community_id=community, server_id=uuid.uuid4())
    other.bedrock_port = 19132
    dst_uow.servers.seed(other)
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store),
        file_store=dst_store,
        bedrock_port_range=PortRange(start=19132, end=19132),
    )
    with pytest.raises(WorkingSetSeedFailedError):
        await imp(
            community_id=CommunityId(community),
            name="imported",
            content=archive,
        )
    # The row and its working set are already committed by the time the plugin
    # import runs (one commit, from create_server); the plugin-import transaction
    # itself never reaches its own commit -- the abort signal is that the commit
    # count stops at 1 (the real UoW rolls the failed transaction back; the fake's
    # staged plugin add is not transactional, mirroring the install-path
    # exhaustion test's same caveat).
    [server] = [s for s in dst_uow.servers.by_id.values() if s.name.value == "imported"]
    assert server.bedrock_port is None
    assert dst_uow.commits == 1


async def test_import_geyser_port_racer_is_seed_failed() -> None:
    # A concurrent allocation racer hitting the UNIQUE(bedrock_port) backstop
    # (issue #1550: the adapter translates the constraint violation to
    # PortAlreadyTakenError at the UPDATE execute site) maps to the same
    # post-commit seed-failure posture as exhaustion -- 503 ``seed_failed``,
    # never an unmapped 500 -- since the server row is already committed here,
    # unlike the install paths' pre-commit 409 ``bedrock_port_taken``.
    class _RacerServerRepository(FakeServerRepository):
        # On this path ``update`` is called only by the Bedrock allocation write
        # (CreateServer uses ``add``), so raising here models the backstop firing.
        async def update(self, server: Server) -> None:
            raise PortAlreadyTakenError(str(server.bedrock_port))

    community = uuid.uuid4()
    archive = await _export_archive(community=community, mod_identifier="Geyser-Spigot")

    dst_uow = FakeUnitOfWork(servers=_RacerServerRepository())
    dst_store = FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store),
        file_store=dst_store,
        bedrock_port_range=_BEDROCK_RANGE,
    )
    with pytest.raises(WorkingSetSeedFailedError):
        await imp(
            community_id=CommunityId(community),
            name="imported",
            content=archive,
        )
    # Same abort signal as the exhaustion test: the plugin-import transaction
    # never reaches its own commit (count stays at 1, from create_server); the
    # real UoW rolls back the staged plugin rows and the failed port UPDATE
    # together (the fake's staged in-memory state is not transactional).
    assert dst_uow.commits == 1


# --- import: lost-update protection (issue #1587) ----------------------------


async def test_import_geyser_does_not_clobber_concurrent_patch() -> None:
    """A concurrent PATCH between create-commit and plugin-import must survive.

    ``_import_plugins`` re-creates the exported Geyser plugin and calls
    ``allocate_bedrock_port_if_geyser``, which writes the whole server entity
    back to the DB. Without a re-fetch under the lifecycle lock, the stale
    server snapshot from create time clobbers a concurrent PATCH's changes
    to ``config`` or ``name``.
    """

    patched_config = {"memory": "4G", "patched": True}

    class _ConcurrentPatchUoW(FakeUnitOfWork):
        async def commit(self) -> None:
            await super().commit()
            if self.commits == 1:
                # Simulate a concurrent PATCH landing after the create commit
                # but before _import_plugins runs: overwrite the stored server's
                # config with the PATCH's payload, using replace() so the held
                # create-time snapshot stays stale.
                for sid, stored in list(self.servers.by_id.items()):
                    self.servers.by_id[sid] = replace(stored, config=patched_config)

    community = uuid.uuid4()
    archive = await _export_archive(community=community, mod_identifier="Geyser-Spigot")

    dst_uow = _ConcurrentPatchUoW()
    dst_store = FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store),
        file_store=dst_store,
        bedrock_port_range=_BEDROCK_RANGE,
    )
    server = await imp(
        community_id=CommunityId(community),
        name="imported",
        content=archive,
    )

    stored = dst_uow.servers.by_id[server.id]
    # The concurrent PATCH's config must survive the plugin import.
    assert stored.config == patched_config
    # The Bedrock port must still be allocated.
    assert stored.bedrock_port == 19132
    # The returned server must carry the current state (API response fidelity):
    # __call__ returns the re-fetched entity, not the stale create-time snapshot.
    assert server.config == patched_config
    assert server.bedrock_port == 19132


async def test_import_plugins_takes_lifecycle_lock() -> None:
    """_import_plugins runs inside the per-server lifecycle lock.

    Mirrors the InstallPlugin / RemovePlugin lock-adoption tests: a recording
    FakeLifecycleLock asserts the lock is acquired and released for the
    imported server's id. The hold is the one the composed CreateServer takes
    (issue #4045).
    """

    community = uuid.uuid4()
    archive = await _export_archive(community=community, mod_identifier="Geyser-Spigot")

    dst_uow = FakeUnitOfWork()
    dst_store = FakeFileStore()
    lock = FakeLifecycleLock()
    # One timeline for the lock and the plugin-row insert (issue #2546): the
    # rows are re-created inside the hold so a concurrent PATCH's whole-entity
    # write-back cannot clobber them (issue #1587). Asserting only
    # [acquire, release] cannot see the insert land between them.
    dst_uow.plugins.events = lock.events
    imp = ImportServer(
        create_server=replace(_create_server(dst_uow, dst_store), lifecycle_lock=lock),
        file_store=dst_store,
        bedrock_port_range=_BEDROCK_RANGE,
    )
    server = await imp(
        community_id=CommunityId(community),
        name="imported",
        content=archive,
    )

    # The plugin insert falls strictly between acquire and release: a mutation
    # that ran the inserts outside the hold would still acquire/release and pass
    # an endpoint-only assert.
    ev = lock.events
    assert (server.id, "add-plugin") in ev, ev
    insert_at = ev.index((server.id, "add-plugin"))
    assert (
        ev.index((server.id, "acquire")) < insert_at < ev.index((server.id, "release"))
    )


async def test_import_plugins_that_lost_the_lock_commits_no_plugin_rows() -> None:
    # The plugin rows (and a Geyser's port) are committed under the lifecycle
    # lock, which can be lost before the commit (issue #3927). The server
    # already exists by then, so the loss is the seed failure the port errors
    # are: a retry would only meet the name the first attempt took.
    community = uuid.uuid4()
    archive = await _export_archive(community=community, mod_identifier="Geyser-Spigot")

    async def _import(dst_uow: FakeUnitOfWork, lock: FakeLifecycleLock) -> None:
        dst_store = FakeFileStore()
        await ImportServer(
            create_server=replace(
                _create_server(dst_uow, dst_store), lifecycle_lock=lock
            ),
            file_store=dst_store,
            bedrock_port_range=_BEDROCK_RANGE,
        )(community_id=CommunityId(community), name="imported", content=archive)

    held_uow = FakeUnitOfWork()
    await _import(held_uow, FakeLifecycleLock())
    lost_uow = FakeUnitOfWork()
    lost = FakeLifecycleLock()
    # The seed's check passes (the archive has no member to publish); the
    # plugin commit's is lost.
    lost.checks_before_loss = 1
    with pytest.raises(WorkingSetSeedFailedError) as exc_info:
        await _import(lost_uow, lost)

    assert isinstance(exc_info.value.__cause__, LifecycleLockLostError)
    # The import's last commit, the plugin rows', is the one the loss stops.
    assert lost_uow.commits == held_uow.commits - 1


async def test_import_publishes_inside_the_lock_its_create_took() -> None:
    # Issue #4045: each member's write publishes on its own, so a start admitted
    # mid-publish would hydrate the archive half-written. The publish runs in
    # the hold the composed CreateServer took before its row committed, with no
    # release in between, and confirms the lock before each write (#3927).
    community = uuid.uuid4()
    dst_uow = FakeUnitOfWork()
    dst_store = FakeFileStore()
    lock = FakeLifecycleLock()
    dst_uow.servers.events = lock.events
    dst_store.events = lock.events
    imp = ImportServer(
        create_server=replace(_create_server(dst_uow, dst_store), lifecycle_lock=lock),
        file_store=dst_store,
    )
    archive = _zip(
        {
            EXPORT_METADATA_FILENAME: _metadata(),
            "world/level.dat": b"level",
            "world/region/r.0.0.mca": b"region",
        }
    )

    server = await imp(
        community_id=CommunityId(community), name="fresh", content=archive
    )

    ev = lock.events
    assert [e for e in ev if e[1] in {"acquire", "release"}] == [
        (server.id, "acquire"),
        (server.id, "release"),
    ], ev
    acquire_at = ev.index((server.id, "acquire"))
    release_at = ev.index((server.id, "release"))
    assert acquire_at < ev.index((server.id, "add-server")), ev
    writes = [i for i, e in enumerate(ev) if e == (server.id, "write-file")]
    # The create's server.properties seed, then the two members.
    assert len(writes) == 3, ev
    assert writes[-1] < release_at, ev
    assert all(ev[i - 1] == (server.id, "check") for i in writes), ev


async def test_import_that_lost_the_lock_before_its_publish_writes_no_member() -> None:
    # A lock lost after the seed stops the publish before its first member's
    # write (issue #3927); the row stands, so it is the seed failure.
    community = uuid.uuid4()
    dst_uow = FakeUnitOfWork()
    dst_store = FakeFileStore()
    lock = FakeLifecycleLock()
    lock.checks_before_loss = 1
    imp = ImportServer(
        create_server=replace(_create_server(dst_uow, dst_store), lifecycle_lock=lock),
        file_store=dst_store,
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(), "world/level.dat": b"x"})

    with pytest.raises(WorkingSetSeedFailedError) as exc_info:
        await imp(community_id=CommunityId(community), name="fresh", content=archive)

    assert isinstance(exc_info.value.__cause__, LifecycleLockLostError)
    assert set(dst_store.files) == {"server.properties"}
    assert len(dst_uow.servers.by_id) == 1


# --- no directory at the root server.properties path (issue #2869) ---------
#
# Import is the sixth door to the directory #2812 describes, and the only one
# that runs on a server with no working set to compare against: the archive
# publishes verbatim, so a member named ``server.properties/x`` would stand a
# DIRECTORY where the platform publishes a file. Same refusal the other five
# give -- 422 ``platform_managed_path`` -- raised in the pre-commit validate
# pass, so a hostile archive leaves NO server row behind (the #277 posture).


async def test_import_member_under_root_properties_creates_no_row() -> None:
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip(
        {
            EXPORT_METADATA_FILENAME: _metadata(),
            "server.properties/notes.txt": b"X",
        }
    )

    with pytest.raises(InvalidFilePathError) as caught:
        await imp(
            community_id=CommunityId(community),
            name="fresh",
            content=archive,
        )
    assert caught.value.reason == "platform_managed_path"
    # Refused BEFORE the row is created, like every other archive property
    # (zip-slip / size / entry count): no row, nothing written (#277).
    assert len(dst_uow.servers.by_id) == 0
    assert dst_store.files == {}


async def test_import_member_under_a_nested_properties_dir_is_published() -> None:
    # Only the ROOT name is guarded. The archive root IS the server root, so the
    # member path is already the joined target; a member under a NESTED
    # ``server.properties`` directory is ordinary user data and still imports.
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip(
        {
            EXPORT_METADATA_FILENAME: _metadata(),
            "backups/server.properties/notes.txt": b"X",
        }
    )

    await imp(
        community_id=CommunityId(community),
        name="fresh",
        content=archive,
    )
    assert dst_store.files["backups/server.properties/notes.txt"] == b"X"


# --- the generation marker's name is reserved at the root (issue #3433) -----
#
# ``.mcsd_generation`` is the Worker's generation marker. A member at that root
# name -- or under it, or carrying its prefix -- would publish a Worker-private
# entry into the new server's store, and as a directory it fails every hydrate.
# The archive is refused with 422 ``reserved_name`` in the pre-commit validate
# pass rather than the member being dropped silently, so no row is left behind.


@pytest.mark.parametrize(
    "member", [".mcsd_generation", ".mcsd_generation/stray.txt", ".mcsd_generation-1"]
)
async def test_import_member_at_the_reserved_marker_name_creates_no_row(
    member: str,
) -> None:
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(), member: b"X"})

    with pytest.raises(InvalidFilePathError) as caught:
        await imp(
            community_id=CommunityId(community),
            name="fresh",
            content=archive,
        )
    assert caught.value.reason == "reserved_name"
    assert len(dst_uow.servers.by_id) == 0
    assert dst_store.files == {}


@pytest.mark.parametrize("member", [".mcsd_generation/", "./.mcsd_generation-cache/"])
async def test_import_directory_member_at_the_reserved_marker_name_creates_no_row(
    member: str,
) -> None:
    # A zip directory header carries no bytes and the write pass skips it, so it
    # never reaches the per-file guard; it is refused all the same, not dropped.
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(), member: b""})

    with pytest.raises(InvalidFilePathError) as caught:
        await imp(
            community_id=CommunityId(community),
            name="fresh",
            content=archive,
        )
    assert caught.value.reason == "reserved_name"
    assert len(dst_uow.servers.by_id) == 0
    assert dst_store.files == {}


async def test_import_nested_directory_member_with_the_marker_name_is_accepted() -> (
    None
):
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip(
        {
            EXPORT_METADATA_FILENAME: _metadata(),
            "world/.mcsd_generation/": b"",
            "world/level.dat": b"L",
        }
    )

    await imp(
        community_id=CommunityId(community),
        name="fresh",
        content=archive,
    )
    assert len(dst_uow.servers.by_id) == 1
    assert dst_store.files["world/level.dat"] == b"L"


async def test_import_member_under_a_nested_marker_name_is_published() -> None:
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip(
        {EXPORT_METADATA_FILENAME: _metadata(), "world/.mcsd_generation": b"X"}
    )

    await imp(
        community_id=CommunityId(community),
        name="fresh",
        content=archive,
    )
    assert dst_store.files["world/.mcsd_generation"] == b"X"


# --- the member-name rule (issue #3973) -------------------------------------
#
# An import publishes every member as a name of the new working set, so a member
# the Worker could not snapshot, or a name no backend can hold, is refused in the
# pre-commit validate pass and no row is left behind. The characters a files-API
# door refuses are not refused here: an export carries whatever names its working
# set held, and importing it again must take them.


def _deep(directories: int) -> str:
    return "/".join(["d"] * directories + ["f"])


def _exactly(size: int) -> str:
    parts: list[str] = []
    while size > 201:
        parts.append("p" * 200)
        size -= 201
    return "/".join([*parts, "p" * size])


@pytest.mark.parametrize(
    ("member", "reason"),
    [
        (_deep(129), "path_too_deep"),
        ("world/" + "n" * 256, "name_too_long"),
        (_exactly(4096), "path_too_long"),
    ],
)
async def test_import_member_the_rule_refuses_creates_no_row(
    member: str, reason: str
) -> None:
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(), member: b"X"})

    with pytest.raises(InvalidFilePathError) as caught:
        await imp(community_id=CommunityId(community), name="fresh", content=archive)
    assert caught.value.reason == reason
    assert len(dst_uow.servers.by_id) == 0
    assert dst_store.files == {}


@pytest.mark.parametrize(
    "member", [_deep(128), "world/" + "n" * 255, _exactly(4095), "a\\b", "a\x85b"]
)
async def test_import_member_the_rule_admits_is_published(member: str) -> None:
    community = uuid.uuid4()
    dst_uow, dst_store = FakeUnitOfWork(), FakeFileStore()
    imp = ImportServer(
        create_server=_create_server(dst_uow, dst_store), file_store=dst_store
    )
    archive = _zip({EXPORT_METADATA_FILENAME: _metadata(), member: b"X"})

    await imp(community_id=CommunityId(community), name="fresh", content=archive)
    assert dst_store.files[member] == b"X"
