"""The member-name rule at every door that creates a name (issues #3969, #3973).

Each files-API door that creates a name refuses one the platform may not create,
with a named 422 reason, before anything is written:

- ``path_too_deep``: more than 128 directory levels, counted as the Worker
  counts them (the root is level 0, only directories count);
- ``name_too_long``: a component over 255 bytes in UTF-8;
- ``path_too_long``: a path over 4,095 bytes;
- ``invalid_name_character``: a character no uploaded file's name may carry.

The cases run the real use cases over the real Storage adapters, fs and object,
so the two backends give one answer. Reads, deletes and a rename away stay open,
so a name that already exists stays reachable.
"""

from __future__ import annotations

import datetime as dt
import io
import tarfile
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.adapters.file_store import StorageFileStoreAdapter
from mc_server_dashboard_api.servers.application.files import (
    DeleteFile,
    ListDir,
    MakeDir,
    ReadFile,
    RenameFile,
    UploadFile,
    WriteFile,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidFilePathError,
    ServerFileNotFoundError,
)
from mc_server_dashboard_api.servers.domain.upload_filename import (
    is_refused_filename_character,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.servers.fakes import FakeControlPlane, FakeFileStore, FakeUnitOfWork
from tests.storage.conftest import build_harness

_NOW = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)


def _nested(directories: int, leaf: str | None = None) -> str:
    """A path of ``directories`` one-letter directories, then ``leaf`` if given."""

    parts = ["d"] * directories
    if leaf is not None:
        parts.append(leaf)
    return "/".join(parts)


def _long_path(size: int) -> str:
    """A path of exactly ``size`` bytes: 200-byte directories, then the rest."""

    parts: list[str] = []
    left = size
    while left > 201:
        parts.append("p" * 200)
        left -= 201
    parts.append("p" * left)
    return "/".join(parts)


def _server(community: uuid.UUID, server: uuid.UUID, *, running: bool) -> Server:
    return Server(
        id=ServerId(server),
        community_id=CommunityId(community),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.RUNNING if running else DesiredState.STOPPED,
        observed_state=ObservedState.RUNNING if running else ObservedState.STOPPED,
        observed_at=_NOW,
        assigned_worker_id=WorkerId(uuid.uuid4()) if running else None,
        created_at=_NOW,
        updated_at=_NOW,
    )


@dataclass
class _Doors:
    """The file use cases over one real backend, for one stopped server."""

    community: CommunityId
    server: ServerId
    store: StorageFileStoreAdapter
    uow: FakeUnitOfWork

    def scope(self) -> dict[str, CommunityId | ServerId]:
        return {"community_id": self.community, "server_id": self.server}

    async def write(self, rel_path: str, content: bytes = b"x") -> None:
        await WriteFile(
            uow=self.uow, control_plane=FakeControlPlane(), file_store=self.store
        )(
            community_id=self.community,
            server_id=self.server,
            rel_path=rel_path,
            content=content,
        )

    async def mkdir(self, rel_path: str) -> None:
        await MakeDir(uow=self.uow, file_store=self.store)(
            community_id=self.community, server_id=self.server, rel_path=rel_path
        )

    async def rename(self, from_path: str, to_path: str) -> None:
        await RenameFile(uow=self.uow, file_store=self.store)(
            community_id=self.community,
            server_id=self.server,
            from_path=from_path,
            to_path=to_path,
        )

    async def upload(
        self, dir_path: str, filename: str, content: bytes, *, extract: bool
    ) -> None:
        await UploadFile(uow=self.uow, file_store=self.store)(
            community_id=self.community,
            server_id=self.server,
            dir_path=dir_path,
            filename=filename,
            content=content,
            extract=extract,
        )

    async def read(self, rel_path: str) -> bytes:
        return await ReadFile(
            uow=self.uow, control_plane=FakeControlPlane(), file_store=self.store
        )(community_id=self.community, server_id=self.server, rel_path=rel_path)

    async def names(self, rel_path: str = ".") -> list[str]:
        listing = await ListDir(
            uow=self.uow, control_plane=FakeControlPlane(), file_store=self.store
        )(community_id=self.community, server_id=self.server, rel_path=rel_path)
        return sorted(entry.name for entry in listing.entries)

    async def delete(self, rel_path: str) -> None:
        await DeleteFile(uow=self.uow, file_store=self.store)(
            community_id=self.community, server_id=self.server, rel_path=rel_path
        )


@pytest.fixture(params=["fs", "object"])
async def doors(request: pytest.FixtureRequest, tmp_path: Path) -> _Doors:
    """A published working set holding ``seed.txt``, on each backend."""

    harness = build_harness(request.param, tmp_path)
    community, server = uuid.uuid4(), uuid.uuid4()
    await harness.publish(
        StorageCommunityId(community), StorageServerId(server), {"seed.txt": b"s"}
    )
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community, server, running=False))
    return _Doors(
        community=CommunityId(community),
        server=ServerId(server),
        store=StorageFileStoreAdapter(storage=harness.storage),
        uow=uow,
    )


async def _refusal(call: Awaitable[None]) -> str:
    """The 422 reason ``call`` is refused with."""

    with pytest.raises(InvalidFilePathError) as caught:
        await call
    return caught.value.reason


# --- depth ------------------------------------------------------------------


async def test_a_write_129_components_deep_is_admitted(doors: _Doors) -> None:
    path = _nested(128, "f")

    await doors.write(path, b"deep")

    assert await doors.read(path) == b"deep"


async def test_a_write_130_components_deep_is_too_deep(doors: _Doors) -> None:
    assert await _refusal(doors.write(_nested(129, "f"))) == "path_too_deep"
    assert await doors.names() == ["seed.txt"]


async def test_a_directory_128_levels_deep_is_made(doors: _Doors) -> None:
    await doors.mkdir(_nested(128))

    assert await doors.names(_nested(128)) == []


async def test_a_directory_129_levels_deep_is_too_deep(doors: _Doors) -> None:
    assert await _refusal(doors.mkdir(_nested(129))) == "path_too_deep"
    assert await doors.names() == ["seed.txt"]


async def test_a_file_renamed_129_components_deep_is_admitted(doors: _Doors) -> None:
    await doors.rename("seed.txt", _nested(128, "f"))

    assert await doors.read(_nested(128, "f")) == b"s"


async def test_a_file_renamed_130_components_deep_is_too_deep(doors: _Doors) -> None:
    assert await _refusal(doors.rename("seed.txt", _nested(129, "f"))) == (
        "path_too_deep"
    )
    assert await doors.read("seed.txt") == b"s"


async def test_a_directory_rename_counts_the_subtree_it_moves(doors: _Doors) -> None:
    # ``src`` holds a directory 127 levels below it. Moved to ``dst`` (level 1)
    # its deepest directory is at level 128; moved to ``a/dst`` (level 2), at 129.
    await doors.mkdir("src/" + _nested(127))

    assert await _refusal(doors.rename("src", "a/dst")) == "path_too_deep"
    assert await doors.names() == ["seed.txt", "src"]

    await doors.rename("src", "dst")
    assert await doors.names("dst/" + _nested(127)) == []


async def test_a_directory_rename_counts_the_files_of_the_subtree(
    doors: _Doors,
) -> None:
    # The file is 128 directories below the destination's parent level once
    # moved: a file may sit at level 128, not 129.
    await doors.write("src/" + _nested(126, "f"))

    assert await _refusal(doors.rename("src", "a/b/dst")) == "path_too_deep"

    await doors.rename("src", "a/dst")
    assert await doors.read("a/dst/" + _nested(126, "f")) == b"x"


async def test_a_directory_rename_measures_the_paths_it_moves(doors: _Doors) -> None:
    # 3,000 bytes below ``src``, so the moved paths are 4,206 bytes long.
    inner = _long_path(3000)
    await doors.write("src/" + inner)
    destination = "/".join(["q" * 200] * 6) + "/dst"

    assert await _refusal(doors.rename("src", destination)) == "path_too_long"
    assert await doors.read("src/" + inner) == b"x"


async def test_an_upload_counts_the_directory_it_lands_in(doors: _Doors) -> None:
    await doors.upload(_nested(128), "f.txt", b"u", extract=False)
    assert await doors.read(_nested(128, "f.txt")) == b"u"

    assert await _refusal(doors.upload(_nested(129), "f.txt", b"u", extract=False)) == (
        "path_too_deep"
    )
    assert await doors.names() == ["d", "seed.txt"]


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _tar_gz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


_Archive = Callable[[dict[str, bytes]], bytes]

_ARCHIVES = [("a.zip", _zip), ("a.tar.gz", _tar_gz)]


@pytest.mark.parametrize(("filename", "archive"), _ARCHIVES)
async def test_an_extract_judges_each_member_joined_to_its_directory(
    doors: _Doors, filename: str, archive: _Archive
) -> None:
    # ``x/y`` adds two levels to every member, so ``d``*127 + ``f`` lands at 129
    # directories. The first member is fine: nothing at all is written.
    content = archive({"ok.txt": b"1", _nested(127, "f"): b"2"})

    assert await _refusal(doors.upload("x/y", filename, content, extract=True)) == (
        "path_too_deep"
    )
    assert await doors.names() == ["seed.txt"]

    content = archive({"ok.txt": b"1", _nested(126, "f"): b"2"})
    await doors.upload("x/y", filename, content, extract=True)
    assert await doors.read("x/y/" + _nested(126, "f")) == b"2"


# --- component length ---------------------------------------------------------


async def test_a_255_byte_name_is_written_and_rewritten(doors: _Doors) -> None:
    # The second write retains the first as a version. On fs both go through a
    # temp sibling, whose name must not be built from the target's (issue #3973).
    name = "n" * 255

    await doors.write(name, b"one")
    await doors.write(name, b"two")

    assert await doors.read(name) == b"two"


async def test_a_255_byte_directory_name_is_made(doors: _Doors) -> None:
    await doors.mkdir("n" * 255)

    assert "n" * 255 in await doors.names()


async def test_a_256_byte_name_is_too_long_at_every_door(doors: _Doors) -> None:
    name = "n" * 256

    assert await _refusal(doors.write(name)) == "name_too_long"
    assert await _refusal(doors.mkdir(name)) == "name_too_long"
    assert await _refusal(doors.rename("seed.txt", name)) == "name_too_long"
    assert await _refusal(doors.upload(name, "f", b"u", extract=False)) == (
        "name_too_long"
    )
    assert await doors.names() == ["seed.txt"]


@pytest.mark.parametrize(("filename", "archive"), _ARCHIVES)
async def test_an_extract_member_of_256_bytes_is_too_long(
    doors: _Doors, filename: str, archive: _Archive
) -> None:
    content = archive({"ok.txt": b"1", "n" * 256: b"2"})

    assert await _refusal(doors.upload(".", filename, content, extract=True)) == (
        "name_too_long"
    )
    assert await doors.names() == ["seed.txt"]


async def test_a_multibyte_name_is_measured_in_utf8_bytes(doors: _Doors) -> None:
    await doors.write("\u3042" * 85, b"kana")
    assert await doors.read("\u3042" * 85) == b"kana"

    assert await _refusal(doors.write("\u3042" * 86)) == "name_too_long"


# --- path length --------------------------------------------------------------


async def test_a_path_over_4095_bytes_is_too_long_at_every_door(doors: _Doors) -> None:
    path = _long_path(4096)

    assert await _refusal(doors.write(path)) == "path_too_long"
    assert await _refusal(doors.mkdir(path)) == "path_too_long"
    assert await _refusal(doors.rename("seed.txt", path)) == "path_too_long"
    assert await doors.names() == ["seed.txt"]


# --- characters ---------------------------------------------------------------

# One of each class the upload rule refuses (issue #3922), and characters it
# admits. Each is expected refused exactly when the upload rule refuses it, so
# the doors and the upload agree on every one.
_CHARACTERS = [
    "\\",  # a backslash
    "\x85",  # C1
    "\ufdd0",  # a noncharacter in U+FDD0..U+FDEF
    "\ufffe",  # the BMP's last two code points
    "\U0001ffff",  # the last two of another plane
    "\ud800",  # a lone surrogate
    " ",
    "\u200b",
    "\u202e",
    "\u0378",  # unassigned
    "\u3042",
    "\U0001f600",
]


@pytest.mark.parametrize("character", _CHARACTERS, ids=lambda c: f"U+{ord(c):04X}")
async def test_the_doors_refuse_the_characters_the_upload_rule_refuses(
    doors: _Doors, character: str
) -> None:
    name = f"a{character}b"
    if not is_refused_filename_character(character):
        await doors.write(name)
        await doors.mkdir(name + "-dir")
        assert await doors.read(name) == b"x"
        return

    assert await _refusal(doors.write(name)) == "invalid_name_character"
    assert await _refusal(doors.write(f"{name}/f")) == "invalid_name_character"
    assert await _refusal(doors.mkdir(name)) == "invalid_name_character"
    assert await _refusal(doors.rename("seed.txt", name)) == "invalid_name_character"
    assert await doors.names() == ["seed.txt"]


@pytest.mark.parametrize(("filename", "archive"), _ARCHIVES)
@pytest.mark.parametrize(
    ("member", "reason"),
    [
        ("a\\b", "invalid_name_character"),
        ("a\x85b", "invalid_name_character"),
        ("a\ufffeb", "invalid_name_character"),
        # Refused by the path's own check, as at every other door, but now
        # before the first member is written.
        ("a\x01b", "invalid_path"),
    ],
)
async def test_an_extract_refuses_a_member_name_no_file_may_carry(
    doors: _Doors, filename: str, archive: _Archive, member: str, reason: str
) -> None:
    content = archive({"ok.txt": b"1", member: b"2"})

    assert await _refusal(doors.upload(".", filename, content, extract=True)) == (
        reason
    )
    assert await doors.names() == ["seed.txt"]


async def test_a_control_character_stays_an_invalid_path(doors: _Doors) -> None:
    # C0 and DEL were refused before the rule, by the path's own check; the
    # doors keep that answer.
    assert await _refusal(doors.write("a\x01b")) == "invalid_path"
    assert await _refusal(doors.mkdir("a\x7fb")) == "invalid_path"


# --- the running write ----------------------------------------------------------


async def test_a_running_write_is_refused_before_it_reaches_the_worker() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community, server, running=True))
    control_plane = FakeControlPlane()
    write = WriteFile(uow=uow, control_plane=control_plane, file_store=FakeFileStore())

    for path, reason in [
        (_nested(129, "f"), "path_too_deep"),
        ("n" * 256, "name_too_long"),
        ("a\\b", "invalid_name_character"),
    ]:
        with pytest.raises(InvalidFilePathError) as caught:
            await write(
                community_id=CommunityId(community),
                server_id=ServerId(server),
                rel_path=path,
                content=b"x",
            )
        assert caught.value.reason == reason
    assert control_plane.dispatched == []


# --- names that already exist stay reachable --------------------------------------


@pytest.fixture(params=["fs", "object"])
async def odd(request: pytest.FixtureRequest, tmp_path: Path) -> _Doors:
    """A working set already holding names the doors refuse, as a snapshot can."""

    harness = build_harness(request.param, tmp_path)
    community, server = uuid.uuid4(), uuid.uuid4()
    await harness.publish(
        StorageCommunityId(community),
        StorageServerId(server),
        {"a\\b": b"odd", _nested(129, "f"): b"deep", "a\x85b": b"c1"},
    )
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community, server, running=False))
    return _Doors(
        community=CommunityId(community),
        server=ServerId(server),
        store=StorageFileStoreAdapter(storage=harness.storage),
        uow=uow,
    )


async def test_an_existing_odd_name_is_read_and_listed(odd: _Doors) -> None:
    assert await odd.read("a\\b") == b"odd"
    assert await odd.read(_nested(129, "f")) == b"deep"
    assert "a\x85b" in await odd.names()


async def test_an_existing_odd_name_is_renamed_away(odd: _Doors) -> None:
    await odd.rename("a\\b", "ab")
    await odd.rename(_nested(129, "f"), "shallow")

    assert await odd.read("ab") == b"odd"
    assert await odd.read("shallow") == b"deep"


async def test_an_existing_odd_name_is_deleted(odd: _Doors) -> None:
    await odd.delete("a\x85b")
    await odd.delete(_nested(129, "f"))

    with pytest.raises(ServerFileNotFoundError):
        await odd.read("a\x85b")
    with pytest.raises(ServerFileNotFoundError):
        await odd.read(_nested(129, "f"))
