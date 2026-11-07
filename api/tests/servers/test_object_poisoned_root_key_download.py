"""The directory download over a snapshot holding a legacy key (issue #3574).

A prior root ``make_dir`` bug left a ``//.dir`` key in some object snapshots
(#1944), and ingest once stored non-canonical keys such as ``world//extra.dat``.
The listing reported either as a directory with an empty name; the download
walk's child path for that name is its parent again, so the walk listed the same
level forever and the zip never ended. These tests drive the real
:class:`ObjectStorage` over the in-memory S3 stub through the servers file seam.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile

import pytest

from mc_server_dashboard_api.servers.adapters.file_store import (
    StorageFileStoreAdapter,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.adapters.object_store import (
    _DIR_MARKER,
    _POINTER,
    ObjectStorage,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory
from tests.storage.helpers import tar_stream


# The unfixed walk never ends; fail fast instead of at the suite-wide cap.
@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "legacy_suffix",
    [
        # The #1944 root marker, hidden as hydrate hides it.
        "/" + _DIR_MARKER,
        # A non-canonical key ingest accepted before #3574, skipped with a warning.
        "world//extra.dat",
    ],
)
async def test_download_dir_of_the_root_zips_the_real_files(legacy_suffix: str) -> None:
    store = FakeS3Store()
    storage = ObjectStorage(fake_s3_factory(store))
    community, server = uuid.uuid4(), uuid.uuid4()
    handle = await storage.begin_snapshot(
        StorageCommunityId(community), StorageServerId(server)
    )
    await storage.write_snapshot(
        handle, tar_stream({"server.properties": b"x", "world/level": b"y"})
    )
    await storage.commit_snapshot(handle)
    server_prefix = f"communities/{community}/servers/{server}/"
    snapshot_prefix = json.loads(store.objects[server_prefix + _POINTER])["snapshot"]
    store.objects[snapshot_prefix + legacy_suffix] = b""
    adapter = StorageFileStoreAdapter(storage=storage)

    stream = adapter.download_dir(
        community_id=CommunityId(community), server_id=ServerId(server), rel_path="."
    )
    blob = b"".join([chunk async for chunk in stream])

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        contents = {name: zf.read(name) for name in zf.namelist()}
    assert contents == {"server.properties": b"x", "world/level": b"y"}
