"""Use-case tests for the resource pack library (issue #1176).

Tests run against fakes (no database), following TESTING.md Section 4.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.application.resource_packs import (
    MAX_RESOURCE_PACK_BYTES,
    DeleteResourcePack,
    DownloadResourcePack,
    ListResourcePacks,
    UploadResourcePack,
)
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    PermissionDeniedError,
    ResourcePackInUseError,
    ResourcePackNotFoundError,
)
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePackAssignment,
    ResourcePackId,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from tests.servers.fakes import (
    FakeClock,
    FakeResourcePackStore,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 16, 12, 0, 0, tzinfo=dt.timezone.utc)
_ZIP_CONTENT = b"PK\x03\x04" + b"\x00" * 100  # minimal zip-like content


def _make_upload(
    uow: FakeUnitOfWork | None = None,
    store: FakeResourcePackStore | None = None,
    clock: FakeClock | None = None,
) -> UploadResourcePack:
    return UploadResourcePack(
        uow=uow or FakeUnitOfWork(),
        store=store or FakeResourcePackStore(),
        clock=clock or FakeClock(_NOW),
    )


class TestUploadResourcePack:
    async def test_upload_happy_path(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeResourcePackStore()
        uc = _make_upload(uow=uow, store=store)
        user_id = uuid.uuid4()

        pack = await uc(
            filename="my-pack.zip",
            display_name="My Pack",
            content=_ZIP_CONTENT,
            uploaded_by=user_id,
        )

        assert pack.filename == "my-pack.zip"
        assert pack.display_name == "My Pack"
        assert pack.size_bytes == len(_ZIP_CONTENT)
        assert pack.uploaded_by == user_id
        assert pack.sha1_hash
        assert pack.sha256_hash
        assert pack.created_at == _NOW
        # Blob stored
        assert pack.id in store.blobs
        # DB row committed
        assert uow.commits == 1
        assert pack.id in uow.resource_packs.packs

    async def test_upload_rejects_non_zip(self) -> None:
        uc = _make_upload()
        with pytest.raises(ValueError, match="zip"):
            await uc(
                filename="my-pack.tar.gz",
                display_name="Bad",
                content=b"data",
                uploaded_by=uuid.uuid4(),
            )

    async def test_upload_rejects_oversized(self) -> None:
        uc = _make_upload()
        big = b"\x00" * (MAX_RESOURCE_PACK_BYTES + 1)
        with pytest.raises(FileTooLargeError):
            await uc(
                filename="big.zip",
                display_name="Too Big",
                content=big,
                uploaded_by=uuid.uuid4(),
            )


class TestListResourcePacks:
    async def test_list_empty(self) -> None:
        uow = FakeUnitOfWork()
        uc = ListResourcePacks(uow=uow)
        assert await uc() == []

    async def test_list_returns_all(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeResourcePackStore()
        upload_uc = _make_upload(uow=uow, store=store)
        user_id = uuid.uuid4()

        await upload_uc(
            filename="a.zip",
            display_name="Alpha",
            content=_ZIP_CONTENT,
            uploaded_by=user_id,
        )
        await upload_uc(
            filename="b.zip",
            display_name="Beta",
            content=_ZIP_CONTENT,
            uploaded_by=user_id,
        )

        uc = ListResourcePacks(uow=uow)
        packs = await uc()
        assert len(packs) == 2
        # Ordered by display_name
        assert packs[0].display_name == "Alpha"
        assert packs[1].display_name == "Beta"


class TestDeleteResourcePack:
    async def test_delete_by_uploader(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeResourcePackStore()
        upload_uc = _make_upload(uow=uow, store=store)
        user_id = uuid.uuid4()

        pack = await upload_uc(
            filename="del.zip",
            display_name="Delete Me",
            content=_ZIP_CONTENT,
            uploaded_by=user_id,
        )

        delete_uc = DeleteResourcePack(uow=uow, store=store)
        await delete_uc(
            resource_pack_id=pack.id,
            caller_id=user_id,
            is_platform_admin=False,
        )

        assert pack.id not in uow.resource_packs.packs
        assert pack.id not in store.blobs

    async def test_delete_by_admin(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeResourcePackStore()
        upload_uc = _make_upload(uow=uow, store=store)
        uploader_id = uuid.uuid4()
        admin_id = uuid.uuid4()

        pack = await upload_uc(
            filename="del.zip",
            display_name="Delete Me",
            content=_ZIP_CONTENT,
            uploaded_by=uploader_id,
        )

        delete_uc = DeleteResourcePack(uow=uow, store=store)
        await delete_uc(
            resource_pack_id=pack.id,
            caller_id=admin_id,
            is_platform_admin=True,
        )

        assert pack.id not in uow.resource_packs.packs

    async def test_delete_denied_for_non_uploader(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeResourcePackStore()
        upload_uc = _make_upload(uow=uow, store=store)

        pack = await upload_uc(
            filename="del.zip",
            display_name="Mine",
            content=_ZIP_CONTENT,
            uploaded_by=uuid.uuid4(),
        )

        delete_uc = DeleteResourcePack(uow=uow, store=store)
        with pytest.raises(PermissionDeniedError):
            await delete_uc(
                resource_pack_id=pack.id,
                caller_id=uuid.uuid4(),
                is_platform_admin=False,
            )

    async def test_delete_not_found(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeResourcePackStore()
        delete_uc = DeleteResourcePack(uow=uow, store=store)

        with pytest.raises(ResourcePackNotFoundError):
            await delete_uc(
                resource_pack_id=ResourcePackId.new(),
                caller_id=uuid.uuid4(),
                is_platform_admin=False,
            )

    async def test_delete_in_use(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeResourcePackStore()
        upload_uc = _make_upload(uow=uow, store=store)
        user_id = uuid.uuid4()

        pack = await upload_uc(
            filename="in-use.zip",
            display_name="In Use",
            content=_ZIP_CONTENT,
            uploaded_by=user_id,
        )

        # Assign the pack to a server
        server_id = ServerId(uuid.uuid4())
        await uow.resource_packs.add_assignment(
            ResourcePackAssignment(
                server_id=server_id,
                resource_pack_id=pack.id,
                require_resource_pack=True,
                resource_pack_prompt=None,
                assigned_by=user_id,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )

        delete_uc = DeleteResourcePack(uow=uow, store=store)
        with pytest.raises(ResourcePackInUseError):
            await delete_uc(
                resource_pack_id=pack.id,
                caller_id=user_id,
                is_platform_admin=False,
            )


class TestDownloadResourcePack:
    async def test_download_happy_path(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeResourcePackStore()
        upload_uc = _make_upload(uow=uow, store=store)

        pack = await upload_uc(
            filename="dl.zip",
            display_name="Download Me",
            content=_ZIP_CONTENT,
            uploaded_by=uuid.uuid4(),
        )

        download_uc = DownloadResourcePack(uow=uow, store=store)
        stream, returned_pack = await download_uc(resource_pack_id=pack.id)

        chunks = [chunk async for chunk in stream]
        assert b"".join(chunks) == _ZIP_CONTENT
        assert returned_pack.filename == "dl.zip"

    async def test_download_not_found(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeResourcePackStore()
        download_uc = DownloadResourcePack(uow=uow, store=store)

        with pytest.raises(ResourcePackNotFoundError):
            await download_uc(resource_pack_id=ResourcePackId.new())
