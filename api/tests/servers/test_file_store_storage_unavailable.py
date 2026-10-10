"""A store outage at the servers file seam, and what a failed write leaves (#3233).

Two things are established here, both through the production adapters — the
:class:`StorageFileStoreAdapter` seam over the real :class:`ObjectStorage`, on
the in-memory S3 stub with an outage injected at a chosen call:

1. **No storage type crosses the seam.** Every ``FileStore`` method reports a
   store outage as :class:`ServerFileStorageUnavailableError`.

2. **What an interrupted write leaves behind, and what a repeat then does.** The
   seam only names the outage; whether a route may answer it 503 — "retry this
   unchanged" — depends on the repeat converging on the state a first-time
   success would have produced. The tests below pin, per operation, the
   aftermath that decision rests on:

   - ``write_file`` / ``make_dir`` / ``retain_if_changed`` converge: the repeat
     finishes the job, generation bump included — with the file's version ring
     full as well as empty. Their routes answer 503.
   - ``delete_file`` / ``delete_dir`` / ``rename_file`` / ``rename_dir`` do NOT
     once the mutation itself has landed: the repeat finds the source gone (a
     miss) or the destination occupied, and the generation the interrupted
     attempt never bumped stays unbumped. Their routes keep the 500.
   - ``rollback`` does NOT either, once the ring is full: capturing the file it
     replaces evicts the oldest version, which may be the one being rolled back
     to, so the repeat is a miss. Its route keeps the 500.

The ``write_file`` case where the file is replaced but the generation is not
bumped is the window issue #3279 describes. It is pinned here as the aftermath
it is, not fixed.

The fs backend is not driven: its file operations raise a raw ``OSError`` for a
device fault and never the storage outage type, so there is nothing for the seam
to translate there.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from starlette.requests import ClientDisconnect
from starlette.types import Message

from mc_server_dashboard_api.http_streaming import ClosingStreamingResponse, started
from mc_server_dashboard_api.servers.adapters.file_store import (
    StorageFileStoreAdapter,
)
from mc_server_dashboard_api.servers.domain.errors import (
    ServerFileNotFoundError,
    ServerFileStorageUnavailableError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.storage.fake_s3 import FakeS3Store
from tests.storage.faulty_s3 import Faults, faulty_s3_factory
from tests.storage.helpers import drain, tar_stream


class _Rig:
    """One published server behind the seam, with the outage switch exposed."""

    def __init__(self) -> None:
        self.backing = FakeS3Store()
        self.faults = Faults()
        self.storage = ObjectStorage(faulty_s3_factory(self.backing, self.faults))
        self.seam = StorageFileStoreAdapter(storage=self.storage)
        self.community = CommunityId(uuid.uuid4())
        self.server = ServerId(uuid.uuid4())

    @property
    def scope(self) -> dict[str, Any]:
        return {"community_id": self.community, "server_id": self.server}

    @property
    def _storage_scope(self) -> tuple[StorageCommunityId, StorageServerId]:
        return (
            StorageCommunityId(self.community.value),
            StorageServerId(self.server.value),
        )

    async def publish(self, files: dict[str, bytes]) -> None:
        handle = await self.storage.begin_snapshot(*self._storage_scope)
        await self.storage.write_snapshot(handle, tar_stream(files))
        await self.storage.commit_snapshot(handle)

    async def generation(self) -> int:
        return await self.storage.current_generation(*self._storage_scope)

    async def read(self, rel_path: str) -> bytes:
        return await self.seam.read_file(**self.scope, rel_path=rel_path)

    async def names(self, rel_path: str = ".") -> set[str]:
        entries = await self.seam.list_dir(**self.scope, rel_path=rel_path)
        return {entry.name for entry in entries}


def _generation_bump(op: str, key: str) -> bool:
    """The last step of every authoritative edit: rewriting the generation marker."""

    return op == "put_object" and key.endswith("/generation")


async def _published() -> _Rig:
    rig = _Rig()
    await rig.publish(
        {
            "server.properties": b"motd=old\n",
            "world/level.dat": b"level",
            "world/session.lock": b"lock",
        }
    )
    return rig


# --- 1. no storage type crosses the seam ------------------------------------


_Call = Callable[[_Rig], Awaitable[object]]

_CALLS: dict[str, _Call] = {
    "read_file": lambda r: r.seam.read_file(**r.scope, rel_path="server.properties"),
    "open_file_stream": lambda r: drain(
        r.seam.open_file_stream(**r.scope, rel_path="server.properties")
    ),
    "list_dir": lambda r: r.seam.list_dir(**r.scope, rel_path="world"),
    "path_exists": lambda r: r.seam.path_exists(**r.scope, rel_path="world"),
    "write_file": lambda r: r.seam.write_file(
        **r.scope, rel_path="server.properties", content=b"motd=new\n"
    ),
    "retain_if_changed": lambda r: r.seam.retain_if_changed(
        **r.scope, rel_path="server.properties"
    ),
    "delete_file": lambda r: r.seam.delete_file(
        **r.scope, rel_path="server.properties"
    ),
    "delete_dir": lambda r: r.seam.delete_dir(**r.scope, rel_path="world"),
    "rename_file": lambda r: r.seam.rename_file(
        **r.scope, from_path="server.properties", to_path="renamed.properties"
    ),
    "rename_dir": lambda r: r.seam.rename_dir(
        **r.scope, from_path="world", to_path="world2"
    ),
    "make_dir": lambda r: r.seam.make_dir(**r.scope, rel_path="plugins"),
    "download_dir": lambda r: drain(r.seam.download_dir(**r.scope, rel_path="world")),
    "export_dir": lambda r: drain(
        r.seam.export_dir(**r.scope, rel_path=".", extra=[("meta.json", b"{}")])
    ),
    "list_versions": lambda r: r.seam.list_versions(
        **r.scope, rel_path="server.properties"
    ),
    "read_version": lambda r: r.seam.read_version(
        **r.scope, rel_path="server.properties", version_id="0" * 20 + "-abcdef01"
    ),
    "rollback": lambda r: r.seam.rollback(
        **r.scope, rel_path="server.properties", version_id="0" * 20 + "-abcdef01"
    ),
}


@pytest.mark.parametrize("method", sorted(_CALLS))
async def test_a_store_outage_crosses_the_seam_as_the_servers_type(method: str) -> None:
    """Whichever ``FileStore`` method the outage strikes, the servers layer sees
    its own error and never a storage one — the seam's documented contract."""

    rig = await _published()
    rig.faults.always()

    with pytest.raises(ServerFileStorageUnavailableError):
        await _CALLS[method](rig)


async def test_dir_zip_outage_while_listing_a_subdirectory_aborts_the_zip() -> None:
    """The zip walk skips a subdirectory that vanished or is refused. One the
    store could not LIST is neither: skipping it would finish a well-formed zip
    that silently lacks a whole subtree."""

    rig = await _published()
    # The requested root lists fine; only the walk's descent into ``world`` fails.
    rig.faults.when = lambda op, key: (
        op == "list_objects" and "/snapshots/" in key and key.endswith("/world/")
    )

    with pytest.raises(ServerFileStorageUnavailableError):
        await drain(rig.seam.download_dir(**rig.scope, rel_path="."))


# --- 2a. writes a repeat converges for (their routes answer 503) ------------


async def test_write_interrupted_before_the_file_is_replaced_changes_nothing() -> None:
    rig = await _published()
    before = await rig.generation()
    rig.faults.when = lambda op, key: (
        op == "put_object" and key.endswith("/server.properties")
    )

    with pytest.raises(ServerFileStorageUnavailableError):
        await _CALLS["write_file"](rig)

    rig.faults.clear()
    assert await rig.read("server.properties") == b"motd=old\n"
    assert await rig.generation() == before


async def test_write_interrupted_at_the_generation_bump_is_finished_by_a_repeat() -> (
    None
):
    """The window of issue #3279: the file is already replaced when the write
    reports the outage, and the generation is not bumped. Repeating the same
    write is what closes it — same bytes, and this time the bump lands."""

    rig = await _published()
    before = await rig.generation()
    rig.faults.when = _generation_bump

    with pytest.raises(ServerFileStorageUnavailableError):
        await _CALLS["write_file"](rig)

    rig.faults.clear()
    assert await rig.read("server.properties") == b"motd=new\n"
    assert await rig.generation() == before

    await _CALLS["write_file"](rig)

    assert await rig.read("server.properties") == b"motd=new\n"
    assert await rig.generation() == before + 1


async def test_first_write_of_an_unpublished_server_is_finished_by_a_repeat() -> None:
    """A never-published server takes the publish path instead of the in-place
    edit. Interrupted at the same last step, it has published the file at
    generation 0; the repeat edits it in place and bumps."""

    rig = _Rig()
    rig.faults.when = _generation_bump

    with pytest.raises(ServerFileStorageUnavailableError):
        await rig.seam.write_file(
            **rig.scope, rel_path="eula.txt", content=b"eula=true\n"
        )

    rig.faults.clear()
    assert await rig.generation() == 0

    await rig.seam.write_file(**rig.scope, rel_path="eula.txt", content=b"eula=true\n")

    assert await rig.read("eula.txt") == b"eula=true\n"
    assert await rig.generation() == 1


async def test_make_dir_interrupted_at_its_generation_bump_is_finished_by_repeat() -> (
    None
):
    rig = await _published()
    before = await rig.generation()
    rig.faults.when = _generation_bump

    with pytest.raises(ServerFileStorageUnavailableError):
        await _CALLS["make_dir"](rig)

    rig.faults.clear()
    assert "plugins" in await rig.names()
    assert await rig.generation() == before

    await _CALLS["make_dir"](rig)

    assert await rig.seam.list_dir(**rig.scope, rel_path="plugins") == []
    assert await rig.generation() == before + 1


async def test_retain_interrupted_after_its_capture_is_a_no_op_on_repeat() -> None:
    """The running-edit snapshot: the version is captured, then pruning the ring
    hits the outage. The repeat finds the newest version already equal to the
    file and captures nothing more."""

    rig = await _published()
    captured = False

    def _after_capture(op: str, key: str) -> bool:
        nonlocal captured
        if op == "copy_object":
            captured = True
            return False
        return captured and op == "list_objects" and "/versions/" in key

    rig.faults.when = _after_capture

    with pytest.raises(ServerFileStorageUnavailableError):
        await _CALLS["retain_if_changed"](rig)

    rig.faults.clear()
    await _CALLS["retain_if_changed"](rig)

    versions = await rig.seam.list_versions(**rig.scope, rel_path="server.properties")
    assert len(versions) == 1
    assert await rig.read("server.properties") == b"motd=old\n"


# --- 2b. writes a repeat does NOT converge for (their routes keep the 500) --


async def test_delete_file_interrupted_after_the_delete_is_a_miss_on_repeat() -> None:
    """The file is gone and the generation unbumped. The repeat cannot finish
    that: it finds nothing to delete and reports the miss — a 404 at the route —
    leaving the generation where the interrupted attempt left it."""

    rig = await _published()
    before = await rig.generation()
    rig.faults.when = _generation_bump

    with pytest.raises(ServerFileStorageUnavailableError):
        await _CALLS["delete_file"](rig)

    rig.faults.clear()
    assert "server.properties" not in await rig.names()

    with pytest.raises(ServerFileNotFoundError):
        await _CALLS["delete_file"](rig)
    assert await rig.generation() == before


async def test_delete_dir_interrupted_after_its_last_object_is_a_miss_on_repeat() -> (
    None
):
    rig = await _published()
    before = await rig.generation()
    rig.faults.when = _generation_bump

    with pytest.raises(ServerFileStorageUnavailableError):
        await _CALLS["delete_dir"](rig)

    rig.faults.clear()
    with pytest.raises(ServerFileNotFoundError):
        await _CALLS["delete_dir"](rig)
    assert await rig.generation() == before


async def test_rename_file_interrupted_between_copy_and_delete_leaves_both_names() -> (
    None
):
    """Source and destination both exist. The rename use case refuses an
    occupied destination, so the repeat is a 409 ``destination_exists`` rather
    than the rename finishing."""

    rig = await _published()
    rig.faults.when = lambda op, key: op == "delete_object"

    with pytest.raises(ServerFileStorageUnavailableError):
        await _CALLS["rename_file"](rig)

    rig.faults.clear()
    assert {"server.properties", "renamed.properties"} <= await rig.names()
    assert await rig.seam.path_exists(**rig.scope, rel_path="renamed.properties")


async def test_rename_file_interrupted_after_the_delete_is_a_miss_on_repeat() -> None:
    rig = await _published()
    before = await rig.generation()
    rig.faults.when = _generation_bump

    with pytest.raises(ServerFileStorageUnavailableError):
        await _CALLS["rename_file"](rig)

    rig.faults.clear()
    with pytest.raises(ServerFileNotFoundError):
        await _CALLS["rename_file"](rig)
    assert await rig.generation() == before


async def test_rename_dir_interrupted_between_copy_and_delete_leaves_both_trees() -> (
    None
):
    rig = await _published()
    rig.faults.when = lambda op, key: op == "delete_object"

    with pytest.raises(ServerFileStorageUnavailableError):
        await _CALLS["rename_dir"](rig)

    rig.faults.clear()
    assert await rig.names("world") == {"level.dat", "session.lock"}
    assert await rig.names("world2") == {"level.dat", "session.lock"}


# --- 2c. the same writes with the file's version ring full -------------------

# The adapter's default retention: a full ring evicts its oldest entry on every
# further capture, which is the state-dependent half of each write's aftermath.
_RING = 10


async def _with_a_full_ring() -> tuple[_Rig, list[str]]:
    """A published server whose ``server.properties`` ring is full, newest first."""

    rig = await _published()
    for n in range(_RING):
        await rig.seam.write_file(
            **rig.scope, rel_path="server.properties", content=b"motd=%d\n" % n
        )
    versions = await rig.seam.list_versions(**rig.scope, rel_path="server.properties")
    assert len(versions) == _RING
    return rig, versions


async def test_write_with_a_full_ring_is_still_finished_by_a_repeat() -> None:
    """The file converges whatever the ring holds: the bytes come from the
    request, not from a retained version. What the repeat costs is history — its
    own capture retains the already-written bytes and evicts one more of the
    oldest versions than a first-time success would have."""

    rig, versions = await _with_a_full_ring()
    before = await rig.generation()
    rig.faults.when = _generation_bump

    with pytest.raises(ServerFileStorageUnavailableError):
        await _CALLS["write_file"](rig)

    rig.faults.clear()
    await _CALLS["write_file"](rig)

    assert await rig.read("server.properties") == b"motd=new\n"
    assert await rig.generation() == before + 1
    after = await rig.seam.list_versions(**rig.scope, rel_path="server.properties")
    assert len(after) == _RING
    # Two captures for one logical write: the two oldest versions are gone.
    assert set(versions[-2:]).isdisjoint(after)
    assert set(versions[:-2]) <= set(after)


async def test_retain_with_a_full_ring_is_still_a_no_op_on_repeat() -> None:
    """The running-edit snapshot with a full ring: the interrupted attempt has
    captured the file, and the repeat's dedup finds that capture, so nothing
    more is retained and nothing more is evicted."""

    rig, versions = await _with_a_full_ring()
    captured = False

    def _after_capture(op: str, key: str) -> bool:
        nonlocal captured
        if op == "copy_object":
            captured = True
            return False
        return captured and op == "list_objects" and "/versions/" in key

    rig.faults.when = _after_capture

    with pytest.raises(ServerFileStorageUnavailableError):
        await _CALLS["retain_if_changed"](rig)

    rig.faults.clear()
    interrupted = await rig.seam.list_versions(
        **rig.scope, rel_path="server.properties"
    )
    await _CALLS["retain_if_changed"](rig)

    assert (
        await rig.seam.list_versions(**rig.scope, rel_path="server.properties")
        == interrupted
    )
    assert set(versions) <= set(interrupted)


async def test_rollback_to_the_oldest_version_of_a_full_ring_is_a_miss_on_repeat() -> (
    None
):
    """Why the rollback keeps its 500. It reads the target version, then writes
    it — and that write first captures the current file, which with a full ring
    evicts the oldest version: the target. Interrupted at the generation bump,
    the restored bytes are readable, the generation is unchanged, and the same
    rollback again finds no such version."""

    rig, versions = await _with_a_full_ring()
    oldest = versions[-1]
    restored = await rig.seam.read_version(
        **rig.scope, rel_path="server.properties", version_id=oldest
    )
    before = await rig.generation()
    rig.faults.when = _generation_bump

    async def _rollback() -> None:
        await rig.seam.rollback(
            **rig.scope, rel_path="server.properties", version_id=oldest
        )

    with pytest.raises(ServerFileStorageUnavailableError):
        await _rollback()

    rig.faults.clear()
    assert await rig.read("server.properties") == restored
    assert await rig.generation() == before

    with pytest.raises(ServerFileNotFoundError):
        await _rollback()
    assert await rig.generation() == before


# --- 3. a closed stream holds nothing (issue #3234) --------------------------


def _holds_nothing(rig: _Rig) -> bool:
    """No reader lease and no store client is still open behind the seam."""

    return rig.storage._leases == {} and rig.faults.open_clients == 0


async def test_closing_a_begun_file_stream_releases_its_lease_at_once() -> None:
    """The seam's stream wraps Storage's. Closing the outer one must close the
    inner one with it — ``async for`` does not — or the reader lease and the
    client stay held until the interpreter finalizes the abandoned generator."""

    rig = await _published()
    stream = rig.seam.open_file_stream(**rig.scope, rel_path="server.properties")
    await anext(stream)
    assert not _holds_nothing(rig)

    await stream.aclose()  # type: ignore[attr-defined]

    assert _holds_nothing(rig)


async def test_closing_a_begun_dir_zip_releases_its_view_and_its_open_member() -> None:
    """Closed while a member is open: the view's lease AND that member's client
    are released at once."""

    rig = await _published()
    stream = rig.seam.download_dir(**rig.scope, rel_path="world")
    await anext(stream)
    assert not _holds_nothing(rig)

    await stream.aclose()  # type: ignore[attr-defined]

    assert _holds_nothing(rig)


async def test_a_dir_zip_that_fails_mid_walk_holds_nothing_afterwards() -> None:
    rig = await _published()
    rig.faults.when = lambda op, key: (
        op == "get_object" and key.endswith("/world/session.lock")
    )

    with pytest.raises(ServerFileStorageUnavailableError):
        await drain(rig.seam.download_dir(**rig.scope, rel_path="world"))

    assert _holds_nothing(rig)


async def test_a_completed_dir_zip_holds_nothing_afterwards() -> None:
    rig = await _published()

    await drain(rig.seam.download_dir(**rig.scope, rel_path="."))

    assert _holds_nothing(rig)


@pytest.mark.parametrize("target", ["file", "directory"])
async def test_a_download_whose_client_disconnects_holds_nothing_afterwards(
    target: str,
) -> None:
    """The route's own composition — the seam's stream, begun, in the closing
    response — with the client gone after the first body chunk. Starlette stops
    there and leaves the body suspended; the response closes it, and the close
    reaches Storage."""

    rig = await _published()
    source = (
        rig.seam.open_file_stream(**rig.scope, rel_path="world/level.dat")
        if target == "file"
        else rig.seam.download_dir(**rig.scope, rel_path="world")
    )
    response = ClosingStreamingResponse(await started(source))
    assert not _holds_nothing(rig)

    async def _receive() -> Message:
        return {"type": "http.disconnect"}

    async def _send(message: Message) -> None:
        if message["type"] == "http.response.body":
            raise OSError("client went away")

    with pytest.raises(ClientDisconnect):
        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}}, _receive, _send
        )

    assert _holds_nothing(rig)
