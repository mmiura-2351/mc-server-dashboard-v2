"""Shared fixtures for the Storage Port contract tests (STORAGE.md Section 7.4).

All three adapter families implement the SAME Port and provide the SAME observable
guarantees (Sections 3, 4); only the realization differs. The backend-agnostic
contract is therefore exercised once, parametrized over BOTH the fs and object
adapters (#105), via the :class:`StorageHarness` the ``harness`` fixture yields.
Backend-specific mechanics (fs symlink layout / fs crash phases; object pointer
flip / prefix sweep) live in their own files against the concrete adapter.

The harness abstracts the two places the adapters legitimately differ at the call
site: constructing the adapter (a tmp dir vs. an in-memory S3 store) and invoking
the adapter-local sweep (sync on fs, async on object). Everything else is the
identical Port surface.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    ServerId,
)
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory
from tests.storage.helpers import tar_stream

# The backends the Port contract is parametrized over (#105). ``remote-fs`` reuses
# the fs adapter (Section 7.2) so it is covered by the fs parametrization.
BACKENDS = ["fs", "object"]


@dataclass
class StorageHarness:
    """One adapter under test plus the two backend-aware call-site helpers."""

    storage: Storage
    _sweep: Callable[[], Awaitable[None]]

    async def publish(
        self,
        community: CommunityId,
        server: ServerId,
        files: dict[str, bytes],
    ) -> None:
        """Stage ``files`` as a snapshot and publish it (the common arrange step)."""

        handle = await self.storage.begin_snapshot(community, server)
        await self.storage.write_snapshot(handle, tar_stream(files))
        await self.storage.commit_snapshot(handle)

    async def sweep(self) -> None:
        await self._sweep()


def build_harness(
    backend: str, tmp_path: Path, *, version_retention: int = 10
) -> StorageHarness:
    """Construct a :class:`StorageHarness` for one backend (fs / object)."""

    if backend == "fs":
        storage: Storage = FsStorage(tmp_path, version_retention=version_retention)

        async def _sweep() -> None:
            assert isinstance(storage, FsStorage)
            storage.sweep()

        return StorageHarness(storage=storage, _sweep=_sweep)

    obj = ObjectStorage(
        fake_s3_factory(FakeS3Store()), version_retention=version_retention
    )
    return StorageHarness(storage=obj, _sweep=obj.sweep)


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    """The backend id under test (parametrized over fs + object)."""

    param: str = request.param
    return param


@pytest.fixture
def harness(backend: str, tmp_path: Path) -> StorageHarness:
    """A :class:`StorageHarness` per backend, default version retention."""

    return build_harness(backend, tmp_path)
