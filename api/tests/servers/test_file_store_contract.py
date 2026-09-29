"""Run the FileStore seam contract against the fast in-memory fake."""

from __future__ import annotations

import uuid

import pytest

from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from tests.contracts.server_file_store import FileStoreContract, FileStoreHarness
from tests.servers.fakes import FakeFileStore


@pytest.fixture
def file_store_harness() -> FileStoreHarness:
    return FileStoreHarness(
        store=FakeFileStore(),
        community_id=CommunityId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
    )


class TestFakeFileStore(FileStoreContract):
    pass
