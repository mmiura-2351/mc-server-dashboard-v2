"""fs read-only integrity-check primitives for the one-shot sweep (issue #744).

The sweep (#744) re-checks artifacts that predate the create/restore gates: it
extracts each backup archive under the decompressed-byte cap and fscks it, and
fscks the on-disk ``current/`` world in place. These two read-only Storage
primitives back that sweep; neither publishes nor mutates ``current`` — they only
report a :class:`WorkingSetReport`. The orchestration (DB health column, audit)
lives in the servers-layer use case; this file pins the Storage side.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.errors import NotFoundError
from mc_server_dashboard_api.storage.domain.value_objects import (
    BackupKey,
    CommunityId,
    ServerId,
)
from tests.storage.helpers import (
    healthy_region_bytes,
    mode_invariant_corrupt_region_bytes,
    new_scope,
    region_targz,
    snapshot_dir,
    tar_stream,
)


async def _publish(
    storage: FsStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
) -> None:
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle)


async def _put_backup(
    storage: FsStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
) -> BackupKey:
    async def _stream() -> AsyncIterator[bytes]:
        yield region_targz(files)

    return await storage.put_backup(community, server, _stream())


async def test_check_backup_health_reports_healthy_for_a_sound_archive(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    key = await _put_backup(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )

    report = await storage.check_backup_health(community, server, key)

    assert report.healthy
    assert report.scanned == 1


async def test_check_backup_health_reports_corrupt_for_a_torn_archive(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    key = await _put_backup(
        storage,
        community,
        server,
        {"world/region/r.0.0.mca": mode_invariant_corrupt_region_bytes()},
    )

    report = await storage.check_backup_health(community, server, key)

    assert not report.healthy
    assert len(report.corrupt) == 1


async def test_check_backup_health_cleans_its_staging(tmp_path: Path) -> None:
    """The extract-and-fsck leaves no staging behind (idempotent, re-runnable)."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    key = await _put_backup(
        storage,
        community,
        server,
        {"world/region/r.0.0.mca": mode_invariant_corrupt_region_bytes()},
    )

    await storage.check_backup_health(community, server, key)
    # A second check yields the identical classification with no drift.
    again = await storage.check_backup_health(community, server, key)

    assert not again.healthy
    server_root = (
        tmp_path / "communities" / str(community.value) / "servers" / str(server.value)
    )
    incoming = server_root / "incoming"
    assert not incoming.exists() or not any(incoming.iterdir())


async def test_check_backup_health_leases_staging_during_fsck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fsck staging dir is pinned by an active-staging lease while it exists, so
    a concurrent orphan-staging sweep skips it instead of _rmtree-ing it mid-extract
    (issue #183), and the lease is dropped once the check returns."""

    from mc_server_dashboard_api.storage.adapters import fs as fs_module
    from mc_server_dashboard_api.storage.integrity.region import (
        WorkingSetReport,
        check_working_set,
    )

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    key = await _put_backup(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )

    leased_during_fsck: list[bool] = []

    def _spy(staging: Path, *, live: bool = False) -> WorkingSetReport:
        # The extracted staging must be pinned at the moment it is being scanned, so a
        # concurrent sweep would skip it (issue #183).
        leased_during_fsck.append(storage._is_staging_active(staging))
        return check_working_set(staging, live=live)

    monkeypatch.setattr(fs_module, "check_working_set", _spy)

    await storage.check_backup_health(community, server, key)

    assert leased_during_fsck == [True]
    # The lease is released in the finally, leaving no dangling pin.
    assert storage._active_staging == set()


async def test_check_backup_health_unknown_key_raises_not_found(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()

    with pytest.raises(NotFoundError):
        await storage.check_backup_health(community, server, BackupKey("missing"))


async def test_check_current_health_reports_healthy_snapshot(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await _publish(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )

    report = await storage.check_current_health(community, server)

    assert report.healthy


async def test_check_current_health_reports_corrupt_snapshot(tmp_path: Path) -> None:
    """A corrupt published snapshot is flagged without mutating ``current``."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await _publish(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    # Tamper the published snapshot in place to model the 2026-06-09 corruption
    # that predates the create gate (a published-then-torn region).
    live = snapshot_dir(tmp_path, community, server)
    (live / "world" / "region" / "r.0.0.mca").write_bytes(
        mode_invariant_corrupt_region_bytes()
    )

    report = await storage.check_current_health(community, server)

    assert not report.healthy
    assert len(report.corrupt) == 1
    # The scan is read-only: current still resolves to the same snapshot.
    assert snapshot_dir(tmp_path, community, server) == live


async def test_check_current_health_unpublished_server_raises_not_found(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()

    with pytest.raises(NotFoundError):
        await storage.check_current_health(community, server)
