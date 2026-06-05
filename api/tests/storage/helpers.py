"""Shared helpers for the storage fs-adapter tests.

Build scopes, turn bytes into the async stream the Port expects, and tar up a
directory tree so a snapshot/backup can be staged from in-memory content.
"""

from __future__ import annotations

import io
import tarfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    ServerId,
)


def new_scope() -> tuple[CommunityId, ServerId]:
    return CommunityId(uuid.uuid4()), ServerId(uuid.uuid4())


async def publish(
    storage: FsStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
) -> None:
    """Stage ``files`` as a snapshot and publish it (the common test arrange step)."""

    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle)


async def stream_of(data: bytes, *, chunk: int = 7) -> AsyncIterator[bytes]:
    """Yield ``data`` in small chunks, exercising the streaming Port contract."""

    for i in range(0, len(data), chunk):
        yield data[i : i + chunk]


def tar_bytes(files: dict[str, bytes]) -> bytes:
    """Build a tar stream whose members are ``{rel_path: content}``."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, content in files.items():
            info = tarfile.TarInfo(name=rel)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


async def tar_stream(
    files: dict[str, bytes], *, chunk: int = 7
) -> AsyncIterator[bytes]:
    async for piece in stream_of(tar_bytes(files), chunk=chunk):
        yield piece


def read_tar(blob: bytes) -> dict[str, bytes]:
    """Inverse of :func:`tar_bytes`: read a tar stream into ``{name: content}``."""

    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tar:
        for member in tar.getmembers():
            if member.isfile():
                handle = tar.extractfile(member)
                assert handle is not None
                out[member.name] = handle.read()
    return out


async def drain(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])


def malicious_tar_with_escape() -> bytes:
    """A tar stream whose member tries to escape via ``../``.

    Used to prove the extractor sandboxes staging (``filter="data"`` refuses it).
    """

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        content = b"pwned"
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def malicious_tar_with_symlink_escape() -> bytes:
    """A tar stream with a symlink member targeting an absolute path outside staging.

    Used to prove ``filter="data"`` rejects/neutralizes a symlink-based escape: it
    refuses a symlink whose target points outside the extraction root.
    """

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="escape_link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    return buf.getvalue()


def bomb_targz(*, decompressed: int = 4096) -> bytes:
    """A self-contained ``tar.gz`` whose single member inflates to ``decompressed``.

    The compressed bytes are tiny (highly compressible zero fill); the decompressed
    member is ``decompressed`` bytes, modeling a gzip bomb so a restore-cap test can
    trip the decompressed-bytes bound with a small fixture (#287).
    """

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"\x00" * decompressed
        info = tarfile.TarInfo(name="world/region.mca")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def snapshot_dir(root: Path, community: CommunityId, server: ServerId) -> Path:
    """The directory ``current`` resolves to (the live snapshot), for assertions."""

    import os

    link = (
        root
        / "communities"
        / str(community.value)
        / "servers"
        / str(server.value)
        / "current"
    )
    target = os.readlink(link)
    return link.parent / target
