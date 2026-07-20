"""Endpoint tests for the worker-authenticated data plane (issue #106).

Exercised in-process via TestClient against a real :class:`FsStorage` over a
tmpdir (the storage seam is the genuine adapter, so the round-trip proves the
archive conventions match end to end). Covers: auth rejection, hydrate
round-trip, the 204 unpublished posture, snapshot atomic publish, partial /
length-mismatch uploads never published, and the Content-Length gate.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.dependencies import (
    get_assigned_worker_lookup,
    get_resolved_jar_lookup,
    get_storage,
    get_transfer_semaphore,
    get_worker_credential,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    RelPath,
    ServerId,
)

_CREDENTIAL = "test-worker-credential"

_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


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


def _setup(
    tmp_path: Path,
    *,
    resolved_jar: str | None = None,
    assigned_worker_id: str | None = None,
    transfer_semaphore: asyncio.Semaphore | None = None,
) -> tuple[TestClient, FsStorage]:
    # Reuse the per-worker shared app; clear overrides on entry so a helper called
    # twice in one test starts clean (the shared_app wrapper clears between tests).
    app = _shared_app
    app.dependency_overrides.clear()
    storage = FsStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: storage

    async def _lookup(_c: uuid.UUID, _s: uuid.UUID) -> str | None:
        return resolved_jar

    app.dependency_overrides[get_resolved_jar_lookup] = lambda: _lookup

    async def _assigned(_c: uuid.UUID, _s: uuid.UUID) -> str | None:
        return assigned_worker_id

    app.dependency_overrides[get_assigned_worker_lookup] = lambda: _assigned
    # The Worker credential the data plane authenticates against is injected via
    # Depends(get_worker_credential) (issue #1753), so the shared app — built with
    # no credential configured — receives the test credential through the override
    # rather than through the environment before build.
    app.dependency_overrides[get_worker_credential] = lambda: _CREDENTIAL
    sem = transfer_semaphore if transfer_semaphore is not None else asyncio.Semaphore(1)
    app.dependency_overrides[get_transfer_semaphore] = lambda: sem
    client = TestClient(app)
    return client, storage


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_CREDENTIAL}"}


def _url(community: uuid.UUID, server: uuid.UUID, suffix: str) -> str:
    return f"/api/data-plane/communities/{community}/servers/{server}/{suffix}"


def _scope() -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4()


async def _publish(
    storage: FsStorage,
    c: uuid.UUID,
    s: uuid.UUID,
    files: dict[str, bytes],
    *,
    publisher: str | None = None,
) -> None:
    handle = await storage.begin_snapshot(CommunityId(c), ServerId(s))

    async def _stream() -> object:
        yield _tar_bytes(files)

    await storage.write_snapshot(handle, _stream())  # type: ignore[arg-type]
    await storage.commit_snapshot(handle, publisher=publisher)


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


async def _store_jar(storage: FsStorage, data: bytes) -> str:
    async def _stream() -> object:
        yield data

    key = await storage.put_jar(_stream())  # type: ignore[arg-type]
    return key.sha256


def test_hydrate_injects_resolved_jar_into_working_set(tmp_path: Path) -> None:
    import asyncio

    jar_bytes = b"PK\x03\x04 resolved server jar"
    sha256 = asyncio.run(_store_jar(FsStorage(tmp_path), jar_bytes))

    client, storage = _setup(tmp_path, resolved_jar=sha256)
    community, server = _scope()
    files = {"server.properties": b"motd=hi", "world/level.dat": b"\x00\x01"}
    asyncio.run(_publish(storage, community, server, files))
    with client:
        resp = client.get(_url(community, server, "working-set"), headers=_auth())
    assert resp.status_code == 200
    members = _read_tar(resp.content)
    # The working set members AND the injected server.jar are present.
    assert members == {**files, "server.jar": jar_bytes}


def test_hydrate_with_jar_but_no_snapshot_sends_jar_only_tar(tmp_path: Path) -> None:
    import asyncio

    jar_bytes = b"PK\x03\x04 jar only"
    sha256 = asyncio.run(_store_jar(FsStorage(tmp_path), jar_bytes))

    client, _ = _setup(tmp_path, resolved_jar=sha256)
    community, server = _scope()
    with client:
        resp = client.get(_url(community, server, "working-set"), headers=_auth())
    assert resp.status_code == 200
    assert _read_tar(resp.content) == {"server.jar": jar_bytes}


def test_hydrate_resolved_jar_absent_from_pool_sends_working_set_alone(
    tmp_path: Path,
) -> None:
    import asyncio

    # A recorded key whose JAR is not actually in the pool: the endpoint falls back
    # to sending the working set alone (the prior posture), not an error.
    missing = "0" * 64
    client, storage = _setup(tmp_path, resolved_jar=missing)
    community, server = _scope()
    files = {"server.properties": b"motd=hi"}
    asyncio.run(_publish(storage, community, server, files))
    with client:
        resp = client.get(_url(community, server, "working-set"), headers=_auth())
    assert resp.status_code == 200
    assert _read_tar(resp.content) == files


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


def test_snapshot_matching_base_generation_publishes(tmp_path: Path) -> None:
    # Issue #847 publish-time generation guard: a Worker that declares the store's
    # CURRENT generation as its base is publishing on top of the latest state, so
    # the publish is allowed.
    import asyncio

    client, storage = _setup(tmp_path)
    community, server = _scope()
    asyncio.run(_publish(storage, community, server, {"keep.txt": b"gen1"}))
    current = asyncio.run(
        storage.current_generation(CommunityId(community), ServerId(server))
    )

    body = _tar_bytes({"world/level.dat": b"gen2"})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            headers={**_auth(), "X-Working-Set-Base-Generation": str(current)},
        )
    assert resp.status_code == 204

    async def _read() -> bytes:
        return b"".join(
            [
                chunk
                async for chunk in storage.open_hydrate_source(
                    CommunityId(community), ServerId(server)
                )
            ]
        )

    assert _read_tar(asyncio.run(_read())) == {"world/level.dat": b"gen2"}


def test_snapshot_stale_base_from_different_publisher_is_refused(
    tmp_path: Path,
) -> None:
    # Issue #847 publish-time generation guard (bug 3): an A->B->A stale-scratch
    # publish. Worker B published the current generation; Worker A then returns with
    # a leftover scratch hydrated from the OLDER generation and tries to publish. The
    # store's current was published by a DIFFERENT worker (B), so A's stale publish is
    # refused (409 stale_generation) BEFORE staging — the prior authoritative copy is
    # untouched.
    import asyncio

    worker_a = str(uuid.uuid4())
    worker_b = str(uuid.uuid4())

    client, storage = _setup(tmp_path)
    community, server = _scope()
    # A published gen1, then B published gen2 (the current authoritative copy).
    asyncio.run(_publish(storage, community, server, {"k": b"g1"}, publisher=worker_a))
    asyncio.run(_publish(storage, community, server, {"k": b"g2"}, publisher=worker_b))
    current = asyncio.run(
        storage.current_generation(CommunityId(community), ServerId(server))
    )

    body = _tar_bytes({"world/level.dat": b"stale"})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            # A declares the OLDER base it still holds and its own id.
            headers={
                **_auth(),
                "X-Working-Set-Base-Generation": str(current - 1),
                "X-Worker-Id": worker_a,
            },
        )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "stale_generation"

    # B's authoritative copy survives the refused publish.
    async def _read() -> bytes:
        return b"".join(
            [
                chunk
                async for chunk in storage.open_hydrate_source(
                    CommunityId(community), ServerId(server)
                )
            ]
        )

    assert _read_tar(asyncio.run(_read())) == {"k": b"g2"}


def test_snapshot_stale_base_from_same_publisher_self_heals(tmp_path: Path) -> None:
    # Issue #847 publish-time generation guard (bug 3): a LOST publish response. The
    # worker published gen N+1 (store advanced) but the HTTP response was lost, so its
    # local marker stayed at base N. Its next publish declares base N < current N+1 —
    # but it is the SAME worker that produced current, so refusing would wedge it
    # forever. The guard allows a stale publish from the publisher of current, so the
    # lost-response case self-heals (the publish lands and advances the store again).
    import asyncio

    worker = str(uuid.uuid4())

    client, storage = _setup(tmp_path)
    community, server = _scope()
    asyncio.run(_publish(storage, community, server, {"k": b"g1"}, publisher=worker))
    asyncio.run(_publish(storage, community, server, {"k": b"g2"}, publisher=worker))
    current = asyncio.run(
        storage.current_generation(CommunityId(community), ServerId(server))
    )

    body = _tar_bytes({"world/level.dat": b"republished"})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            # Same worker re-publishing with its stale base after a lost response.
            headers={
                **_auth(),
                "X-Working-Set-Base-Generation": str(current - 1),
                "X-Worker-Id": worker,
            },
        )
    assert resp.status_code == 204

    async def _read() -> bytes:
        return b"".join(
            [
                chunk
                async for chunk in storage.open_hydrate_source(
                    CommunityId(community), ServerId(server)
                )
            ]
        )

    assert _read_tar(asyncio.run(_read())) == {"world/level.dat": b"republished"}


def test_snapshot_in_flight_stale_publish_refused_after_restore(
    tmp_path: Path,
) -> None:
    # Issue #873 restore-clobber window: a worker published the current generation,
    # then an API restore replaced ``current/`` (bumping the generation and stamping
    # the RESTORE_PUBLISHER sentinel). A final snapshot from that worker is still in
    # flight, declaring its OLD base generation. Because the restore bumped the
    # generation AND recorded a DIFFERENT publisher (the sentinel), the publish guard
    # refuses (409 stale_generation) — the just-restored data is NOT clobbered.
    import asyncio

    from mc_server_dashboard_api.storage.domain.value_objects import BackupKey

    worker = str(uuid.uuid4())

    client, storage = _setup(tmp_path)
    community, server = _scope()
    c, s = CommunityId(community), ServerId(server)

    # The worker published gen1, captured as a backup we will restore later.
    asyncio.run(_publish(storage, community, server, {"k": b"g1"}, publisher=worker))
    key: BackupKey = asyncio.run(storage.create_backup_from_current(c, s))
    # The worker advanced to gen2 (its scratch is now at gen2).
    asyncio.run(_publish(storage, community, server, {"k": b"g2"}, publisher=worker))
    base = asyncio.run(storage.current_generation(c, s))

    # An API restore of the gen1 backup replaces current/ and bumps the generation.
    asyncio.run(storage.restore_backup(c, s, key))

    # The worker's in-flight final snapshot declares its base (gen2) and own id.
    body = _tar_bytes({"k": b"in-flight"})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            headers={
                **_auth(),
                "X-Working-Set-Base-Generation": str(base),
                "X-Worker-Id": worker,
            },
        )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "stale_generation"

    # The restored data survives the refused publish (not clobbered).
    async def _read() -> bytes:
        return b"".join([chunk async for chunk in storage.open_hydrate_source(c, s)])

    assert _read_tar(asyncio.run(_read())) == {"k": b"g1"}


def test_snapshot_in_flight_stale_publish_refused_after_edit(tmp_path: Path) -> None:
    # Issue #889 edit-clobber window (the issue's direction 2): a worker published the
    # current generation, then an authoritative API file edit mutated ``current/`` in
    # place. The SAME worker's final snapshot is still in flight, declaring its OLD base
    # generation — and crucially the worker WAS the last publisher, so before the fix
    # the guard (base == current, same publisher) would PASS and clobber the edit. The
    # edit now bumps the generation AND stamps the API_EDIT_PUBLISHER sentinel, so the
    # in-flight snapshot sees base < current published by a DIFFERENT publisher and the
    # guard refuses it (409 stale_generation) — the edit survives.
    import asyncio

    worker = str(uuid.uuid4())

    client, storage = _setup(tmp_path)
    community, server = _scope()
    c, s = CommunityId(community), ServerId(server)

    # The worker published, and is the recorded publisher; its scratch is at this gen.
    asyncio.run(_publish(storage, community, server, {"k": b"snap"}, publisher=worker))
    base = asyncio.run(storage.current_generation(c, s))

    # An authoritative API edit mutates current/ in place and bumps the generation.
    asyncio.run(storage.write_file(c, s, RelPath("k"), b"edited"))

    # The worker's in-flight final snapshot declares its base (pre-edit gen) and own id.
    body = _tar_bytes({"k": b"in-flight"})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            headers={
                **_auth(),
                "X-Working-Set-Base-Generation": str(base),
                "X-Worker-Id": worker,
            },
        )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "stale_generation"

    # The edited data survives the refused publish (not clobbered).
    async def _read() -> bytes:
        return b"".join([chunk async for chunk in storage.open_hydrate_source(c, s)])

    assert _read_tar(asyncio.run(_read())) == {"k": b"edited"}


def test_snapshot_refused_when_edit_lands_during_upload_window(
    tmp_path: Path,
) -> None:
    # Issue #899: the pre-stream guard PASSES (the worker declares the store's current
    # base), but an at-rest edit lands DURING the upload window — after the guard read
    # current and before commit. The commit-time expected-base re-check catches the
    # advance and refuses (409 stale_generation, the same contract as the pre-stream
    # refusal); the staging is discarded and the just-edited current survives.
    import asyncio

    worker = str(uuid.uuid4())

    client, storage = _setup(tmp_path)
    community, server = _scope()
    c, s = CommunityId(community), ServerId(server)

    asyncio.run(_publish(storage, community, server, {"k": b"snap"}, publisher=worker))
    base = asyncio.run(storage.current_generation(c, s))

    # Simulate the at-rest edit landing in the upload window by hooking the guard's
    # current_generation read: the FIRST call (the guard) returns the base, then
    # mutates current/ in place so the store advances past it before the commit's
    # re-check. The commit re-reads the now-advanced generation and refuses.
    real_current_generation = storage.current_generation
    edited = False

    async def _hooked_current_generation(
        community_id: CommunityId, server_id: ServerId
    ) -> int:
        nonlocal edited
        value = await real_current_generation(community_id, server_id)
        if not edited:
            edited = True
            await storage.write_file(c, s, RelPath("k"), b"edited-mid-upload")
        return value

    storage.current_generation = _hooked_current_generation  # type: ignore[method-assign]

    body = _tar_bytes({"k": b"in-flight"})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            headers={
                **_auth(),
                # The worker declares the CURRENT base — the pre-stream guard passes.
                "X-Working-Set-Base-Generation": str(base),
                "X-Worker-Id": worker,
            },
        )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "stale_generation"

    storage.current_generation = real_current_generation  # type: ignore[method-assign]

    # The edit that landed in the window survives; the stale worker upload is discarded.
    async def _read() -> bytes:
        return b"".join([chunk async for chunk in storage.open_hydrate_source(c, s)])

    assert _read_tar(asyncio.run(_read())) == {"k": b"edited-mid-upload"}


def test_snapshot_refused_when_edit_lands_during_upload_window_without_base_header(
    tmp_path: Path,
) -> None:
    # Issue #920 finding 2: a publish that declares NO base generation (older worker /
    # never hydrated) must STILL get the commit-time re-check — the expected base is
    # derived server-side from the guard's reading regardless of the header, so the
    # upload-window clobber is closed on this route too. Previously the no-base path
    # passed expected_base=None and skipped the re-check, leaving the window open.
    import asyncio

    client, storage = _setup(tmp_path)
    community, server = _scope()
    c, s = CommunityId(community), ServerId(server)

    asyncio.run(_publish(storage, community, server, {"k": b"snap"}))
    base = asyncio.run(storage.current_generation(c, s))

    # Same upload-window hook as the declared-base test: the guard's current_generation
    # read returns the base, then mutates current/ in place so the store advances before
    # the commit's re-check.
    real_current_generation = storage.current_generation
    edited = False

    async def _hooked_current_generation(
        community_id: CommunityId, server_id: ServerId
    ) -> int:
        nonlocal edited
        value = await real_current_generation(community_id, server_id)
        if not edited:
            edited = True
            await storage.write_file(c, s, RelPath("k"), b"edited-mid-upload")
        return value

    storage.current_generation = _hooked_current_generation  # type: ignore[method-assign]

    body = _tar_bytes({"k": b"in-flight"})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            # No X-Working-Set-Base-Generation header: the no-base-claim route.
            content=body,
            headers=_auth(),
        )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "stale_generation"
    # The 409 carries the guard-time current (= expected_base) as base_generation.
    assert resp.json()["base_generation"] == base

    storage.current_generation = real_current_generation  # type: ignore[method-assign]

    # The edit that landed in the window survives; the stale worker upload is discarded.
    async def _read() -> bytes:
        return b"".join([chunk async for chunk in storage.open_hydrate_source(c, s)])

    assert _read_tar(asyncio.run(_read())) == {"k": b"edited-mid-upload"}


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


def test_snapshot_empty_upload_is_rejected_and_not_published(tmp_path: Path) -> None:
    import asyncio

    client, storage = _setup(tmp_path)
    community, server = _scope()
    asyncio.run(_publish(storage, community, server, {"keep.txt": b"prior"}))

    # An empty tar (just the end-of-archive marker, no members) carries a matching
    # Content-Length, so it clears the length gate but stages zero files: a worker
    # packing an empty working set is a bug signal, not a publishable snapshot
    # (STORAGE.md Section 4.1). It must be refused loudly and leave the prior
    # authoritative copy intact.
    body = _tar_bytes({})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"), content=body, headers=_auth()
        )
    assert resp.status_code == 400
    assert resp.json()["reason"] == "empty_snapshot"

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


def test_snapshot_corrupt_region_is_refused_with_machine_readable_reason(
    tmp_path: Path,
) -> None:
    import asyncio

    client, storage = _setup(tmp_path)
    community, server = _scope()
    asyncio.run(_publish(storage, community, server, {"keep.txt": b"prior"}))

    # A byte-complete upload (clears the length gate) whose ``.mca`` is structurally
    # corrupt — a location entry past EOF, modelling a crash-during-save tear (#703).
    # The content-integrity gate (#739) must refuse the publish with a non-2xx and a
    # machine-readable reason, and leave the prior snapshot. (A merely non-4096-aligned
    # size is the normal unpadded tail under the single rule set, no longer corruption
    # — issue #927 — so the fixture must be a real tear.)
    body = _tar_bytes({"world/region/r.0.0.mca": _corrupt_mca()})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"), content=body, headers=_auth()
        )
    assert resp.status_code == 422
    payload = resp.json()
    assert payload["reason"] == "working_set_corrupt"
    assert payload["corrupt_count"] == 1

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


def _unaligned_live_mca(tail: int = 459) -> bytes:
    """A region with the legitimate UNPADDED tail of a 26.x world (#923/#927): an
    8 KiB header plus one chunk in sector 2 ending ``tail`` bytes in, so the size is
    not a 4096 multiple but the trailing chunk fits byte-precisely. The single rule
    set accepts it at every gate."""
    offset = 2
    size = offset * 4096 + tail
    image = bytearray(size)
    image[0:4] = offset.to_bytes(3, "big") + bytes([1])
    length = size - offset * 4096 - 4
    start = offset * 4096
    image[start : start + 4] = length.to_bytes(4, "big")
    image[start + 4] = 2  # zlib.
    return bytes(image)


def _corrupt_mca() -> bytes:
    """A genuinely torn region: a 3-sector aligned file whose location entry points
    past EOF (sector_out_of_bounds). The single rule set refuses it at every gate
    (issue #927)."""
    image = bytearray(3 * 4096)
    image[0:4] = (4).to_bytes(3, "big") + bytes([1])  # offset 4, count 1: past EOF.
    return bytes(image)


def test_snapshot_publishes_unaligned_working_set_without_source_header(
    tmp_path: Path,
) -> None:
    # The #927 acceptance case: a working set whose region has the legitimate unpadded
    # tail of a 26.x world must PUBLISH with NO X-Snapshot-Source header at all (the
    # mode header is removed end-to-end). This is exactly the stop-leg checkpoint the
    # old strict rule refused after a sweep-stop timeout / SIGKILL / crash.
    client, storage = _setup(tmp_path)
    community, server = _scope()
    body = _tar_bytes({"world/region/r.0.0.mca": _unaligned_live_mca()})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            headers=_auth(),
        )
    assert resp.status_code == 204

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
    assert "world/region/r.0.0.mca" in _read_tar(published)


def test_snapshot_refuses_genuinely_corrupt_working_set(tmp_path: Path) -> None:
    # A genuinely torn region (location entry past EOF) is still refused by the
    # content-integrity gate (issue #927: the single rule set's byte-precise check
    # catches realistic tears), and current/ keeps the prior good snapshot.
    client, storage = _setup(tmp_path)
    community, server = _scope()
    asyncio.run(_publish(storage, community, server, {"keep.txt": b"prior"}))

    body = _tar_bytes({"world/region/r.0.0.mca": _corrupt_mca()})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"), content=body, headers=_auth()
        )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "working_set_corrupt"

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


def test_snapshot_partial_region_loss_is_refused_with_machine_readable_reason(
    tmp_path: Path,
) -> None:
    import asyncio

    client, storage = _setup(tmp_path)
    community, server = _scope()
    healthy_mca = bytes(2 * 4096)  # a structurally valid empty region.
    asyncio.run(
        _publish(
            storage,
            community,
            server,
            {
                "world/region/r.0.0.mca": healthy_mca,
                "world/region/r.0.1.mca": healthy_mca,
            },
        )
    )

    # A byte-complete, structurally-clean upload that DROPS a region file from a
    # dimension that still exists (issue #854): the missing-region gate must refuse
    # the publish with 422 ``working_set_incomplete`` and leave the prior snapshot.
    body = _tar_bytes({"world/region/r.0.0.mca": healthy_mca})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"), content=body, headers=_auth()
        )
    assert resp.status_code == 422
    payload = resp.json()
    assert payload["reason"] == "working_set_incomplete"
    assert payload["affected_count"] == 1
    # The recovery (STORAGE.md) needs the LOST NAMES: a bounded per-directory list
    # of the missing region files must be surfaced so the operator can delete them
    # from ``current/`` and re-publish. Here the loss is small, so nothing truncated.
    assert payload["directories"] == [
        {"directory": "world/region", "missing": ["r.0.1.mca"]}
    ]
    assert payload["truncated"] is False

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
    assert _read_tar(published) == {
        "world/region/r.0.0.mca": healthy_mca,
        "world/region/r.0.1.mca": healthy_mca,
    }


def test_snapshot_partial_region_loss_report_is_bounded_and_truncated(
    tmp_path: Path,
) -> None:
    import asyncio

    from mc_server_dashboard_api.dataplane.api import transfers

    client, storage = _setup(tmp_path)
    community, server = _scope()
    healthy_mca = bytes(2 * 4096)  # a structurally valid empty region.

    # Publish many region dirs, each with more region files than the per-directory
    # name cap, so a drop of all-but-one from every dir exceeds BOTH caps and the
    # surfaced list must be bounded and flagged truncated.
    dir_count = transfers._MISSING_REGION_DIR_CAP + 5
    names_per_dir = transfers._MISSING_REGION_NAME_CAP + 5
    prior: dict[str, bytes] = {}
    for d in range(dir_count):
        for n in range(names_per_dir):
            prior[f"world/dim{d:03d}/region/r.{n}.0.mca"] = healthy_mca
    asyncio.run(_publish(storage, community, server, prior))

    # Re-publish keeping only the FIRST region file of each dir: every dir is a
    # partial loss (some-but-not-all gone).
    kept = {f"world/dim{d:03d}/region/r.0.0.mca": healthy_mca for d in range(dir_count)}
    body = _tar_bytes(kept)
    with client:
        resp = client.post(
            _url(community, server, "snapshot"), content=body, headers=_auth()
        )
    assert resp.status_code == 422
    payload = resp.json()
    assert payload["reason"] == "working_set_incomplete"
    assert payload["affected_count"] == dir_count
    # The body must be BOUNDED: at most the dir cap, each at most the name cap.
    assert len(payload["directories"]) == transfers._MISSING_REGION_DIR_CAP
    for entry in payload["directories"]:
        assert len(entry["missing"]) <= transfers._MISSING_REGION_NAME_CAP
    # Both caps fired, so the list is flagged partial.
    assert payload["truncated"] is True


def test_snapshot_partial_region_loss_report_exactly_at_cap_not_truncated(
    tmp_path: Path,
) -> None:
    import asyncio

    from mc_server_dashboard_api.dataplane.api import transfers

    client, storage = _setup(tmp_path)
    community, server = _scope()
    healthy_mca = bytes(2 * 4096)

    # Publish exactly the cap counts so no cap fires (strict > in the builder).
    dir_count = transfers._MISSING_REGION_DIR_CAP  # 20
    names_per_dir = transfers._MISSING_REGION_NAME_CAP  # 50
    prior: dict[str, bytes] = {}
    for d in range(dir_count):
        for n in range(names_per_dir):
            prior[f"world/dim{d:03d}/region/r.{n}.0.mca"] = healthy_mca
    asyncio.run(_publish(storage, community, server, prior))

    # Keep only the first region file of each dir: exactly cap dirs, each with
    # exactly (names_per_dir - 1) missing names — at the cap, not over.
    kept = {f"world/dim{d:03d}/region/r.0.0.mca": healthy_mca for d in range(dir_count)}
    body = _tar_bytes(kept)
    with client:
        resp = client.post(
            _url(community, server, "snapshot"), content=body, headers=_auth()
        )
    assert resp.status_code == 422
    payload = resp.json()
    assert payload["reason"] == "working_set_incomplete"
    # Exactly at cap: all dirs are surfaced and truncated must be False.
    assert len(payload["directories"]) == dir_count
    assert payload["truncated"] is False


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


# --- per-chunk idle timeout (issue #1699) ------------------------------------


def test_byte_counter_raises_on_chunk_idle_timeout() -> None:
    """A stalling source triggers _ChunkIdleTimeout after the per-chunk deadline."""
    from mc_server_dashboard_api.dataplane.api.transfers import (
        _ByteCounter,
        _ChunkIdleTimeout,
    )

    async def _stalling_source() -> object:
        yield b"first-chunk"
        # Hang forever on the second chunk — simulates a partitioned worker.
        await asyncio.sleep(3600)
        yield b"never-reached"  # pragma: no cover

    counter = _ByteCounter(
        _stalling_source(),  # type: ignore[arg-type]
        declared=1024,
        chunk_idle_timeout=0.05,
    )

    async def _consume() -> None:
        async for _ in counter.stream():
            pass

    with pytest.raises(_ChunkIdleTimeout):
        asyncio.run(_consume())


def test_byte_counter_normal_stream_unaffected_by_idle_timeout() -> None:
    """A stream that delivers chunks promptly is unaffected by the idle timeout."""
    from mc_server_dashboard_api.dataplane.api.transfers import _ByteCounter

    data = b"hello-world"

    async def _fast_source() -> object:
        yield data

    counter = _ByteCounter(
        _fast_source(),  # type: ignore[arg-type]
        declared=len(data),
        chunk_idle_timeout=5.0,
    )
    chunks: list[bytes] = []

    async def _consume() -> list[bytes]:
        async for chunk in counter.stream():
            chunks.append(chunk)
        return chunks

    asyncio.run(_consume())
    assert b"".join(chunks) == data
    assert counter.count == len(data)


def test_snapshot_chunk_idle_timeout_aborts_staging_and_returns_408(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint aborts the snapshot handle and returns 408 on chunk idle timeout."""
    from mc_server_dashboard_api.dataplane.api import transfers

    # Shrink the timeout so the test completes fast; the endpoint's body reader
    # will time out on the stalling source injected below.
    monkeypatch.setattr(transfers, "_CHUNK_IDLE_TIMEOUT", 0.05)

    client, storage = _setup(tmp_path)
    community, server = _scope()
    asyncio.run(_publish(storage, community, server, {"keep.txt": b"prior"}))

    # Replace the request body stream with one that stalls after the first chunk.
    # Since TestClient sends all content at once, we monkeypatch _ByteCounter to
    # inject a stalling source regardless of the real request body.
    real_init = transfers._ByteCounter.__init__

    def _stalling_init(
        self: transfers._ByteCounter,
        source: object,
        declared: int,
        **kwargs: object,
    ) -> None:
        async def _stalling() -> object:
            yield b"x" * 10
            await asyncio.sleep(3600)
            yield b"unreachable"  # pragma: no cover

        real_init(self, _stalling(), declared=declared, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(transfers._ByteCounter, "__init__", _stalling_init)

    body = _tar_bytes({"world/level.dat": b"new"})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"), content=body, headers=_auth()
        )
    assert resp.status_code == 408
    assert resp.json()["reason"] == "chunk_idle_timeout"

    # The prior authoritative copy survives the aborted upload.
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


def test_hydrate_resolved_jar_supersedes_stale_embedded_jar(tmp_path: Path) -> None:
    """The resolved JAR must overwrite the stale embedded server.jar in the snapshot.

    Regression test for issue #1942: when a working set embeds its own
    ``server.jar`` (from a prior snapshot), a version change must still take
    effect — the hydrate tar must contain exactly ONE ``server.jar`` with the
    resolved content, not the stale embedded one.
    """
    import asyncio

    jar_bytes = b"PK\x03\x04 resolved JAR B (version 1.22)"
    sha256 = asyncio.run(_store_jar(FsStorage(tmp_path), jar_bytes))

    client, storage = _setup(tmp_path, resolved_jar=sha256)
    community, server = _scope()
    # The working set embeds a STALE server.jar (from a prior snapshot that
    # ran version 1.21). The hydrate must NOT serve it.
    files = {
        "server.jar": b"PK\x03\x04 stale JAR A (version 1.21)",
        "server.properties": b"motd=hi",
        "world/level.dat": b"\x00\x01",
    }
    asyncio.run(_publish(storage, community, server, files))
    with client:
        resp = client.get(_url(community, server, "working-set"), headers=_auth())
    assert resp.status_code == 200
    members = _read_tar(resp.content)
    # Exactly one server.jar in the output, carrying the RESOLVED content.
    assert members["server.jar"] == jar_bytes
    # Other working-set files still round-trip.
    assert members["server.properties"] == b"motd=hi"
    assert members["world/level.dat"] == b"\x00\x01"


def test_hydrate_generation_header_matches_leased_snapshot(tmp_path: Path) -> None:
    """Issue #1954: the generation header must match the served content.

    Previously, a standalone ``current_generation`` read BEFORE the hydrate stream
    leased the snapshot left a window where a concurrent bump mislabeled the served
    bytes. After the fix, generation is read atomically with the lease, so the header
    always matches the content's generation.
    """
    import asyncio

    client, storage = _setup(tmp_path)
    community, server = _scope()
    c, s = CommunityId(community), ServerId(server)

    asyncio.run(_publish(storage, community, server, {"f": b"v1"}))
    gen1 = asyncio.run(storage.current_generation(c, s))

    with client:
        resp = client.get(_url(community, server, "working-set"), headers=_auth())
    assert resp.status_code == 200
    assert resp.headers["X-Working-Set-Generation"] == str(gen1)

    # Publish a second snapshot and verify the generation header advances.
    asyncio.run(_publish(storage, community, server, {"f": b"v2"}))
    gen2 = asyncio.run(storage.current_generation(c, s))
    assert gen2 > gen1

    with client:
        resp = client.get(_url(community, server, "working-set"), headers=_auth())
    assert resp.status_code == 200
    assert resp.headers["X-Working-Set-Generation"] == str(gen2)
    assert _read_tar(resp.content) == {"f": b"v2"}


def test_hydrate_generation_header_on_204_is_zero(tmp_path: Path) -> None:
    """On 204 (no published snapshot) the generation header is 0."""
    client, _ = _setup(tmp_path)
    community, server = _scope()
    with client:
        resp = client.get(_url(community, server, "working-set"), headers=_auth())
    assert resp.status_code == 204
    assert resp.headers["X-Working-Set-Generation"] == "0"


# --- assignment-aware publisher guard (issue #1703) ----------------------------


def test_snapshot_stale_base_from_assigned_worker_is_allowed(tmp_path: Path) -> None:
    # Issue #1703 unwedge: worker A published the current generation (a late final
    # snapshot after being re-placed). Worker B is now the assigned worker and tries
    # to publish with base < current (because A advanced the store after B hydrated).
    # The guard sees current_publisher=A != publisher=B, but B IS the assigned worker,
    # so the publish must be ALLOWED — refusing it would wedge B's entire session.
    import asyncio

    worker_a = str(uuid.uuid4())
    worker_b = str(uuid.uuid4())

    client, storage = _setup(tmp_path, assigned_worker_id=worker_b)
    community, server = _scope()
    # A published gen1, then A published gen2 (late final snapshot).
    asyncio.run(_publish(storage, community, server, {"k": b"g1"}, publisher=worker_a))
    asyncio.run(_publish(storage, community, server, {"k": b"g2"}, publisher=worker_a))
    current = asyncio.run(
        storage.current_generation(CommunityId(community), ServerId(server))
    )

    body = _tar_bytes({"world/level.dat": b"from-b"})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            headers={
                **_auth(),
                "X-Working-Set-Base-Generation": str(current - 1),
                "X-Worker-Id": worker_b,
            },
        )
    assert resp.status_code == 204

    async def _read() -> bytes:
        return b"".join(
            [
                chunk
                async for chunk in storage.open_hydrate_source(
                    CommunityId(community), ServerId(server)
                )
            ]
        )

    assert _read_tar(asyncio.run(_read())) == {"world/level.dat": b"from-b"}


def test_snapshot_commit_fenced_when_assignment_moved_during_upload(
    tmp_path: Path,
) -> None:
    # Issue #1703 fence: worker A's upload starts with base==current (pre-stream guard
    # passes). During the upload the server is re-placed on B (assignment changed).
    # At commit time the fence re-reads the assignment: assigned=B != publisher=A, so
    # the staging is aborted and A's late commit never wedges B's session.
    import asyncio

    worker_a = str(uuid.uuid4())
    worker_b = str(uuid.uuid4())

    # The assignment lookup is hooked to return worker_b (simulating re-placement
    # during the upload window). The pre-stream guard passes because base==current.
    client, storage = _setup(tmp_path, assigned_worker_id=worker_b)
    community, server = _scope()
    asyncio.run(_publish(storage, community, server, {"k": b"g1"}, publisher=worker_a))
    current = asyncio.run(
        storage.current_generation(CommunityId(community), ServerId(server))
    )

    body = _tar_bytes({"world/level.dat": b"late-from-a"})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            headers={
                **_auth(),
                "X-Working-Set-Base-Generation": str(current),
                "X-Worker-Id": worker_a,
            },
        )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "publisher_not_assigned"

    # The prior authoritative copy survives.
    async def _read() -> bytes:
        return b"".join(
            [
                chunk
                async for chunk in storage.open_hydrate_source(
                    CommunityId(community), ServerId(server)
                )
            ]
        )

    assert _read_tar(asyncio.run(_read())) == {"k": b"g1"}


def test_snapshot_late_publish_after_clear_with_no_replacement_still_lands(
    tmp_path: Path,
) -> None:
    # Regression guard: when no worker is currently assigned (the stale-stop arm
    # cleared the assignment, and no new placement happened yet), a publish from the
    # old worker must still land — the fence is permissive when assigned is None,
    # because refusing would discard the final snapshot the held-assignment window
    # was designed to protect. This case is the normal path for a stop whose final
    # snapshot completes within the grace window.
    import asyncio

    worker_a = str(uuid.uuid4())

    # assigned_worker_id=None simulates the cleared-no-replacement state.
    client, storage = _setup(tmp_path, assigned_worker_id=None)
    community, server = _scope()
    asyncio.run(_publish(storage, community, server, {"k": b"g1"}, publisher=worker_a))
    current = asyncio.run(
        storage.current_generation(CommunityId(community), ServerId(server))
    )

    body = _tar_bytes({"world/level.dat": b"final-from-a"})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            headers={
                **_auth(),
                "X-Working-Set-Base-Generation": str(current),
                "X-Worker-Id": worker_a,
            },
        )
    assert resp.status_code == 204

    async def _read() -> bytes:
        return b"".join(
            [
                chunk
                async for chunk in storage.open_hydrate_source(
                    CommunityId(community), ServerId(server)
                )
            ]
        )

    assert _read_tar(asyncio.run(_read())) == {"world/level.dat": b"final-from-a"}


# --- hydrate send deadline (issue #1822) --------------------------------------


def test_hydrate_send_stall_releases_reader_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled ASGI send triggers _ChunkSendTimeout and releases the lease."""
    from mc_server_dashboard_api.dataplane.api.transfers import (
        _ChunkSendTimeout,
        _DeadlineStreamingResponse,
    )

    client, storage = _setup(tmp_path)
    community, server = _scope()
    # Publish a working set large enough to produce at least one body chunk.
    files = {"world/level.dat": b"x" * 200_000}
    asyncio.run(_publish(storage, community, server, files))

    # Exercise the response's stream_response directly with a stalling send,
    # bypassing middleware (which proxies the send through internal queues).
    # This proves the mechanism: the send deadline fires, the body iterator is
    # closed, and the generator's finally releases the lease.
    async def _drive() -> None:
        hydrate_source = storage.open_hydrate_source(
            CommunityId(community), ServerId(server)
        )
        resp = _DeadlineStreamingResponse(
            hydrate_source,
            server_id=server,
            media_type="application/x-tar",
            send_timeout=0.05,
        )

        from collections.abc import MutableMapping
        from typing import Any

        async def stalling_send(message: MutableMapping[str, Any]) -> None:
            if message.get("type") == "http.response.body" and message.get("body"):
                await asyncio.sleep(3600)

        with pytest.raises(_ChunkSendTimeout):
            await asyncio.wait_for(resp.stream_response(stalling_send), timeout=5.0)

    asyncio.run(_drive())

    # The reader lease must have been released (the generator's finally ran).
    # FsStorage exposes _leases; an empty dict means all leases are released.
    assert storage._leases == {}


def test_hydrate_send_no_false_positive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal fast send delivers the full body without triggering a timeout."""
    from mc_server_dashboard_api.dataplane.api import transfers

    # Even with a short timeout, a fast send must not fire.
    monkeypatch.setattr(transfers, "_CHUNK_SEND_TIMEOUT", 5.0)

    client, storage = _setup(tmp_path)
    community, server = _scope()
    files = {"world/level.dat": b"y" * 100_000}
    asyncio.run(_publish(storage, community, server, files))
    with client:
        resp = client.get(_url(community, server, "working-set"), headers=_auth())
    assert resp.status_code == 200
    assert _read_tar(resp.content) == files


def test_close_propagation_replay(tmp_path: Path) -> None:
    """Closing _replay propagates to the inner iterator (lease release)."""

    closed = False

    async def _source() -> object:
        nonlocal closed
        try:
            yield b"first-chunk"
            yield b"second-chunk"
        finally:
            closed = True

    from mc_server_dashboard_api.dataplane.api.transfers import _prime

    async def _drive() -> None:
        nonlocal closed
        primed = await _prime(_source())  # type: ignore[arg-type]
        # Pull one chunk then close early.
        chunk = await primed.__anext__()
        assert chunk == b"first-chunk"
        aclose = getattr(primed, "aclose", None)
        assert aclose is not None
        await aclose()
        assert closed

    asyncio.run(_drive())


def test_close_propagation_with_jar_member(tmp_path: Path) -> None:
    """Closing _with_jar_member propagates to the inner working_set iterator."""

    closed = False

    async def _source() -> object:
        nonlocal closed
        try:
            yield b"ws-chunk-1"
            yield b"ws-chunk-2"
        finally:
            closed = True

    from mc_server_dashboard_api.dataplane.api.transfers import _with_jar_member

    async def _drive() -> None:
        nonlocal closed
        gen = _with_jar_member(_source(), b"jar-member-bytes")  # type: ignore[arg-type]
        # Pull the jar member and one ws chunk, then close early.
        chunk1 = await gen.__anext__()
        assert chunk1 == b"jar-member-bytes"
        chunk2 = await gen.__anext__()
        assert chunk2 == b"ws-chunk-1"
        aclose = getattr(gen, "aclose", None)
        assert aclose is not None
        await aclose()
        assert closed

    asyncio.run(_drive())


# --- transfer semaphore release tests (issue #1696) --------------------------


def test_semaphore_released_after_hydrate(tmp_path: Path) -> None:
    """The semaphore slot is released after the hydrate stream is consumed."""
    sem = asyncio.Semaphore(1)
    client, storage = _setup(tmp_path, transfer_semaphore=sem)
    community, server = _scope()
    files = {"server.properties": b"motd=hi"}
    asyncio.run(_publish(storage, community, server, files))
    with client:
        resp = client.get(_url(community, server, "working-set"), headers=_auth())
    assert resp.status_code == 200
    # After the response is fully consumed the semaphore must be back at 1.
    assert not sem.locked()


def test_semaphore_released_after_hydrate_204(tmp_path: Path) -> None:
    """The semaphore is released on the 204 (no-snapshot) early-return path."""
    sem = asyncio.Semaphore(1)
    client, _ = _setup(tmp_path, transfer_semaphore=sem)
    community, server = _scope()
    with client:
        resp = client.get(_url(community, server, "working-set"), headers=_auth())
    assert resp.status_code == 204
    assert not sem.locked()


def test_semaphore_released_after_snapshot_success(tmp_path: Path) -> None:
    """The semaphore is released after a successful snapshot publish."""
    sem = asyncio.Semaphore(1)
    client, _ = _setup(tmp_path, transfer_semaphore=sem)
    community, server = _scope()
    body = _tar_bytes({"server.properties": b"motd=hi"})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            headers={**_auth(), "Content-Length": str(len(body))},
        )
    assert resp.status_code == 204
    assert not sem.locked()


def test_semaphore_released_after_snapshot_length_mismatch(tmp_path: Path) -> None:
    """The semaphore is released when a snapshot fails due to length mismatch."""
    sem = asyncio.Semaphore(1)
    client, _ = _setup(tmp_path, transfer_semaphore=sem)
    community, server = _scope()
    body = _tar_bytes({"server.properties": b"motd=hi"})
    with client:
        resp = client.post(
            _url(community, server, "snapshot"),
            content=body,
            headers={**_auth(), "Content-Length": str(len(body) + 100)},
        )
    assert resp.status_code == 400
    assert not sem.locked()
