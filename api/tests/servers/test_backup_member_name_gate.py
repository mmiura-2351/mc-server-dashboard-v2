"""The backup upload gate and the member-name rule (issue #3973).

A restore publishes every member of an uploaded backup as a name of the working
set, so the gate refuses a member the Worker could not snapshot (more than 128
directory levels, or a path over 4,095 bytes) or no backend can hold (a
component over 255 bytes). It answers 422 ``invalid_archive``, its ``detail``
naming the member and the reason.

The characters the files-API doors refuse are not refused here. A running
server can create any name its game or a plugin writes, and the platform backs
it up as it is: a backup the platform wrote must upload again.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.application.backups import (
    _validate_backup_archive,
)
from mc_server_dashboard_api.servers.application.files import MAX_ARCHIVE_ENTRIES
from mc_server_dashboard_api.servers.domain.errors import InvalidBackupArchiveError
from tests.storage.conftest import build_harness
from tests.storage.helpers import drain, new_scope


def _deep(directories: int, leaf: str | None = "f") -> str:
    return "/".join(["d"] * directories + ([leaf] if leaf is not None else []))


def _exactly(size: int) -> str:
    parts: list[str] = []
    while size > 201:
        parts.append("p" * 200)
        size -= 201
    return "/".join([*parts, "p" * size])


def _targz(*members: tarfile.TarInfo) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for info in members:
            tar.addfile(info, io.BytesIO(b"x") if info.isfile() else None)
    return buf.getvalue()


def _file(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = 1
    return info


def _directory(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info


@pytest.mark.parametrize(
    ("member", "reason"),
    [
        (_file(_deep(129)), "path_too_deep"),
        (_directory(_deep(129, None)), "path_too_deep"),
        (_file("world/" + "n" * 256), "name_too_long"),
        (_file(_exactly(4096)), "path_too_long"),
    ],
    ids=["file-too-deep", "directory-too-deep", "name-too-long", "path-too-long"],
)
def test_the_gate_refuses_a_member_the_rule_refuses(
    member: tarfile.TarInfo, reason: str
) -> None:
    with pytest.raises(InvalidBackupArchiveError) as caught:
        _validate_backup_archive(_targz(member), max_entries=10)

    assert str(caught.value).startswith("unsafe member: ")
    assert str(caught.value).endswith(f" ({reason})")


@pytest.mark.parametrize(
    "member",
    [
        _file(_deep(128)),
        _directory(_deep(128, None)),
        _file("world/" + "n" * 255),
        _file(_exactly(4095)),
        # Names a files-API door refuses: a backslash, C1, a noncharacter.
        _file("a\\b"),
        _file("a\x85b"),
        _file("a\uffffb"),
    ],
    ids=["file", "directory", "name", "path", "backslash", "c1", "noncharacter"],
)
def test_the_gate_admits_a_member_the_rule_admits(member: tarfile.TarInfo) -> None:
    _validate_backup_archive(_targz(member), max_entries=10)


def test_a_name_that_is_not_utf8_is_measured_in_its_own_bytes() -> None:
    # tarfile hands a byte that is not UTF-8 over as a surrogate escape. 255 such
    # bytes are a 255-byte component, as the Worker counts it, not a refusal and
    # not an archive the gate cannot read.
    _validate_backup_archive(_targz(_file("\udcff" * 255)), max_entries=10)

    with pytest.raises(InvalidBackupArchiveError) as caught:
        _validate_backup_archive(_targz(_file("\udcff" * 256)), max_entries=10)
    assert str(caught.value).endswith(" (name_too_long)")


@pytest.mark.parametrize("backend", ["fs", "object"])
async def test_a_backup_of_names_a_door_refuses_uploads_again(
    backend: str, tmp_path: Path
) -> None:
    # A running server's snapshot can hold such names; the backup of them is
    # the platform's own archive.
    harness = build_harness(backend, tmp_path)
    storage = harness.storage
    community, server = new_scope()
    names = {"a\\b": b"1", "a\x85b": b"2", "a\ufffeb": b"3", _deep(128): b"4"}
    await harness.publish(community, server, names)

    key = await storage.create_backup_from_current(community, server)
    archive = await drain(storage.open_backup(community, server, key))

    _validate_backup_archive(archive, max_entries=MAX_ARCHIVE_ENTRIES)
