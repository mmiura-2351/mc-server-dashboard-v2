"""Endpoint tests for the worker-authenticated data plane (issue #106).

Exercised in-process via TestClient against a real :class:`FsStorage` over a
tmpdir (the storage seam is the genuine adapter, so the round-trip proves the
archive conventions match end to end). Covers: auth rejection, hydrate
round-trip, the 204 unpublished posture, snapshot atomic publish, partial /
length-mismatch uploads never published, and the Content-Length gate.
"""

from __future__ import annotations

import io
import tarfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.dependencies import get_storage
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    ServerId,
)

_CREDENTIAL = "test-worker-credential"


@pytest.fixture(autouse=True)
def _worker_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", _CREDENTIAL)


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, content in files.items():
            info = tarfile.TarInfo(name=rel)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _read_tar(blob: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tar:
        for member in tar.getmembers():
            if member.isfile():
                handle = tar.extractfile(member)
                assert handle is not None
                out[member.name] = handle.read()
    return out


def _setup(tmp_path: Path) -> tuple[TestClient, FsStorage]:
    storage = FsStorage(tmp_path)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    client = TestClient(app)
    return client, storage


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_CREDENTIAL}"}


def _url(community: uuid.UUID, server: uuid.UUID, suffix: str) -> str:
    return f"/data-plane/communities/{community}/servers/{server}/{suffix}"


def _scope() -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4()


async def _publish(
    storage: FsStorage, c: uuid.UUID, s: uuid.UUID, files: dict[str, bytes]
) -> None:
    handle = await storage.begin_snapshot(CommunityId(c), ServerId(s))

    async def _stream() -> object:
        yield _tar_bytes(files)

    await storage.write_snapshot(handle, _stream())  # type: ignore[arg-type]
    await storage.commit_snapshot(handle)


def test_hydrate_rejects_missing_credential(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    community, server = _scope()
    with client:
        resp = client.get(_url(community, server, "working-set"))
    assert resp.status_code == 401


def test_hydrate_rejects_wrong_credential(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    community, server = _scope()
    with client:
        resp = client.get(
            _url(community, server, "working-set"),
            headers={"Authorization": "Bearer wrong"},
        )
    assert resp.status_code == 401


def test_snapshot_rejects_missing_credential(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    community, server = _scope()
    with client:
        resp = client.post(_url(community, server, "snapshot"), content=b"x")
    assert resp.status_code == 401


def test_hydrate_round_trips_the_published_working_set(tmp_path: Path) -> None:
    import asyncio

    client, storage = _setup(tmp_path)
    community, server = _scope()
    files = {"server.properties": b"motd=hi", "world/level.dat": b"\x00\x01"}
    asyncio.run(_publish(storage, community, server, files))
    with client:
        resp = client.get(_url(community, server, "working-set"), headers=_auth())
    assert resp.status_code == 200
    assert _read_tar(resp.content) == files


def test_hydrate_unpublished_server_is_204(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    community, server = _scope()
    with client:
        resp = client.get(_url(community, server, "working-set"), headers=_auth())
    assert resp.status_code == 204


def test_snapshot_publishes_atomically(tmp_path: Path) -> None:
    import asyncio

    client, storage = _setup(tmp_path)
    community, server = _scope()
    body = _tar_bytes({"world/level.dat": b"new-world"})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"), content=body, headers=_auth()
        )
    assert resp.status_code == 204

    # The published working set is exactly what was uploaded.
    async def _read() -> bytes:
        return b"".join(
            [
                chunk
                async for chunk in storage.open_hydrate_source(
                    CommunityId(community), ServerId(server)
                )
            ]
        )

    published = asyncio.run(_read())
    assert _read_tar(published) == {"world/level.dat": b"new-world"}


def test_snapshot_length_mismatch_is_not_published(tmp_path: Path) -> None:
    import asyncio

    client, storage = _setup(tmp_path)
    community, server = _scope()
    asyncio.run(_publish(storage, community, server, {"keep.txt": b"prior"}))

    body = _tar_bytes({"world/level.dat": b"partial"})
    # Claim a longer Content-Length than the body actually carries: the streamed
    # byte count will not match, so the snapshot must be refused and the prior
    # authoritative copy preserved (FR-DATA-6).
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            headers={**_auth(), "Content-Length": str(len(body) + 10)},
        )
    # A truncated body against an over-long Content-Length surfaces as the client
    # request being rejected; either the length-mismatch 400 or the transport
    # aborting the over-claimed read. In both cases the prior snapshot survives.
    assert resp.status_code in (400, 500)

    async def _read() -> bytes:
        return b"".join(
            [
                chunk
                async for chunk in storage.open_hydrate_source(
                    CommunityId(community), ServerId(server)
                )
            ]
        )

    published = asyncio.run(_read())
    assert _read_tar(published) == {"keep.txt": b"prior"}


def test_snapshot_under_declared_length_aborts_mid_stream(tmp_path: Path) -> None:
    import asyncio

    client, storage = _setup(tmp_path)
    community, server = _scope()
    asyncio.run(_publish(storage, community, server, {"keep.txt": b"prior"}))

    body = _tar_bytes({"world/level.dat": b"more-than-declared"})
    # Declare fewer bytes than the body carries: the counter must trip as soon as
    # the streamed count passes the declared length, aborting before the over-long
    # body is spooled in full, and the prior authoritative copy must survive.
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            headers={**_auth(), "Content-Length": str(len(body) - 10)},
        )
    assert resp.status_code == 400

    async def _read() -> bytes:
        return b"".join(
            [
                chunk
                async for chunk in storage.open_hydrate_source(
                    CommunityId(community), ServerId(server)
                )
            ]
        )

    published = asyncio.run(_read())
    assert _read_tar(published) == {"keep.txt": b"prior"}


def test_snapshot_over_cap_body_aborts_mid_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from mc_server_dashboard_api.dataplane.api import transfers

    # Shrink the cap so a small body can exercise the mid-stream cap abort: the
    # declared length is under the cap (passes the header gate) but the streamed
    # bytes run past it, so the counter must trip to a 413 and preserve the prior
    # snapshot.
    monkeypatch.setattr(transfers, "_MAX_SNAPSHOT_BYTES", 8)

    client, storage = _setup(tmp_path)
    community, server = _scope()
    asyncio.run(_publish(storage, community, server, {"keep.txt": b"prior"}))

    body = b"x" * 64
    # Declare exactly the cap (passes the `> cap` header gate) while the body runs
    # well past it, so the abort can only come from the mid-stream cap check.
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            headers={**_auth(), "Content-Length": "8"},
        )
    assert resp.status_code == 413

    async def _read() -> bytes:
        return b"".join(
            [
                chunk
                async for chunk in storage.open_hydrate_source(
                    CommunityId(community), ServerId(server)
                )
            ]
        )

    published = asyncio.run(_read())
    assert _read_tar(published) == {"keep.txt": b"prior"}


def test_snapshot_requires_content_length(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    community, server = _scope()

    # A chunked (no Content-Length) upload is refused by the proven-complete gate.
    def _chunks() -> Iterator[bytes]:
        yield _tar_bytes({"x.txt": b"y"})

    with client:
        resp = client.post(
            _url(community, server, "snapshot"), content=_chunks(), headers=_auth()
        )
    assert resp.status_code == 411
