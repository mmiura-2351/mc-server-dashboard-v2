"""Run the FileStore seam contract over both production Storage adapters."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.adapters.file_store import StorageFileStoreAdapter
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from tests.contracts.server_file_store import FileStoreContract, FileStoreHarness
from tests.storage.conftest import build_harness


@pytest.fixture(params=["fs", "object"])
def file_store_harness(
    request: pytest.FixtureRequest, tmp_path: Path
) -> FileStoreHarness:
    return FileStoreHarness(
        store=StorageFileStoreAdapter(
            storage=build_harness(request.param, tmp_path).storage
        ),
        community_id=CommunityId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
    )


class TestStorageFileStoreAdapter(FileStoreContract):
    pass
