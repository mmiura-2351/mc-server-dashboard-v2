"""Live object-backend contract tests against a real S3-compatible endpoint.

Operationalizes the ``object`` backend on SeaweedFS (issue #702). The adapter's
behaviour is otherwise proven against the in-memory stub; this module exercises
the load-bearing STORAGE.md Section 4.2/7.3 assumptions against a *real* endpoint
so a backend that violates one is caught before it ships as the default:

1. **Read-after-write on overwrite PUT** of the single ``current.json`` pointer
   object — the atomic publish flip depends on last-writer-wins + read-after-write.
2. **Server-side CopyObject** — both publish and file-version retention copy
   objects server-side; a copy that round-trips bytes would still pass here, but a
   copy that silently no-ops or corrupts would not.
3. **Multipart upload + prefix ListObjectsV2** — every working-set member is
   uploaded via multipart and listed by prefix.

It also exercises the startup ``sweep`` path against the real endpoint, covering
the #916-item-4 question: SeaweedFS's ListMultipartUploads omits the optional
``Initiated`` timestamp, which the adapter tolerates (see ``object_client``).

Gated on ``MCD_TEST_S3_ENDPOINT`` (with ``MCD_TEST_S3_BUCKET`` /
``MCD_TEST_S3_ACCESS_KEY`` / ``MCD_TEST_S3_SECRET_KEY``); skipped cleanly when
unset so ``make check`` / CI stay green without an S3 instance. Run locally with a
throwaway SeaweedFS — see ``docs/dev/DEPLOYMENT.md`` "Running the live SeaweedFS
contract tests".
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.storage.adapters.object_client import (
    make_s3_client_factory,
)
from mc_server_dashboard_api.storage.adapters.object_store import (
    ObjectStorage,
    S3ClientFactory,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    RelPath,
    ServerId,
)
from tests.storage.helpers import (
    drain,
    healthy_region_bytes,
    new_scope,
    read_tar,
    tar_stream,
)

_ENDPOINT = os.environ.get("MCD_TEST_S3_ENDPOINT")
_BUCKET = os.environ.get("MCD_TEST_S3_BUCKET", "mcsd")
_ACCESS_KEY = os.environ.get("MCD_TEST_S3_ACCESS_KEY", "mcsdaccess")
_SECRET_KEY = os.environ.get("MCD_TEST_S3_SECRET_KEY", "mcsdsecret")

pytestmark = pytest.mark.skipif(
    _ENDPOINT is None,
    reason="MCD_TEST_S3_ENDPOINT not set (no live S3 endpoint)",
)


def _factory() -> S3ClientFactory:
    assert _ENDPOINT is not None
    return make_s3_client_factory(
        endpoint=_ENDPOINT,
        bucket=_BUCKET,
        access_key=_ACCESS_KEY,
        secret_key=_SECRET_KEY,
    )


def _scope() -> tuple[CommunityId, ServerId]:
    # A fresh scope per test so concurrent/repeated runs against one bucket are
    # disjoint (mirroring the per-run scratch DB pattern).
    return new_scope()


async def test_pointer_overwrite_put_is_read_after_write() -> None:
    # Hard requirement 1: a single-object overwrite PUT of the pointer object is
    # immediately visible (last-writer-wins + read-after-write). This is the one
    # atomic step the publish flip relies on.
    factory = _factory()
    key = f"communities/{uuid.uuid4().hex}/current.json"
    async with factory() as client:
        await client.put_object(key, b'{"snapshot":"A"}')
        assert await _read(client, key) == b'{"snapshot":"A"}'
        await client.put_object(key, b'{"snapshot":"B"}')
        assert await _read(client, key) == b'{"snapshot":"B"}'
        await client.delete_object(key)


async def test_copy_object_is_server_side_and_faithful() -> None:
    # Hard requirement 2: server-side CopyObject produces a byte-identical object
    # under the destination key (the publish + version-retention primitive).
    factory = _factory()
    base = f"communities/{uuid.uuid4().hex}/"
    src, dst = base + "src/r.0.0.mca", base + "dst/r.0.0.mca"
    body = healthy_region_bytes()
    async with factory() as client:
        await client.put_object(src, body)
        await client.copy_object(src, dst)
        assert await _read(client, dst) == body
        await client.delete_object(src)
        await client.delete_object(dst)


async def test_multipart_upload_and_prefix_list() -> None:
    # Hard requirement 3: a multipart upload (>= 2 parts) round-trips faithfully and
    # the object is found by a prefix ListObjectsV2 scan.
    factory = _factory()
    prefix = f"communities/{uuid.uuid4().hex}/snap/"
    key = prefix + "big.bin"
    chunks = [b"a" * (6 * 1024 * 1024), b"b" * (3 * 1024 * 1024)]

    async def parts() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    async with factory() as client:
        await client.upload_multipart(key, parts())
        assert await _read(client, key) == b"".join(chunks)
        listed = await client.list_objects(prefix)
        assert any(obj.key == key for obj in listed)
        await client.delete_object(key)


async def test_full_publish_hydrate_cycle() -> None:
    # The end-to-end Port path against the real endpoint: stage a working set,
    # publish it through the pointer flip, hydrate it back, and read a file. This
    # exercises CopyObject + multipart + pointer-flip together as the adapter wires
    # them.
    storage = ObjectStorage(_factory())
    community, server = _scope()
    files = {
        "server.properties": b"x=1\n",
        "world/region/r.0.0.mca": healthy_region_bytes(),
    }
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle)

    hydrated = read_tar(await drain(storage.open_hydrate_source(community, server)))
    assert hydrated == files
    assert (
        await storage.read_file(community, server, RelPath("server.properties"))
        == b"x=1\n"
    )

    await storage.prune_to_final_snapshot(community, server)


async def test_startup_sweep_tolerates_orphan_multipart_upload() -> None:
    # The #916-item-4 verification: a crash-leftover in-progress multipart upload is
    # listed by SeaweedFS WITHOUT an ``Initiated`` timestamp. The startup sweep must
    # complete (not crash on the missing field) and must NOT abort the upload (its
    # age is treated as "now", below the abort threshold), degrading orphan
    # reclamation to the bucket lifecycle rule.
    factory = _factory()
    storage = ObjectStorage(factory)
    community, server = _scope()
    key = (
        f"communities/{community.value}/servers/{server.value}/"
        "incoming/orphan/region/r.0.0.mca"
    )
    async with factory() as client:
        created = await client._client.create_multipart_upload(  # type: ignore[attr-defined]
            Bucket=_BUCKET, Key=key
        )
        upload_id = created["UploadId"]
        await client._client.upload_part(  # type: ignore[attr-defined]
            Bucket=_BUCKET,
            Key=key,
            UploadId=upload_id,
            PartNumber=1,
            Body=b"z" * (6 * 1024 * 1024),
        )
    try:
        # Must not raise (pre-fix this crashed with KeyError('Initiated')).
        await storage.sweep()
        async with factory() as client:
            uploads = await client.list_multipart_uploads(
                f"communities/{community.value}/"
            )
        assert any(u.upload_id == upload_id for u in uploads)
    finally:
        async with factory() as client:
            await client.abort_multipart_upload(key, upload_id)


async def _read(client: object, key: str) -> bytes:
    body = await client.get_object(key)  # type: ignore[attr-defined]
    return b"".join([chunk async for chunk in body])
