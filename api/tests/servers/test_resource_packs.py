"""Use-case tests for the resource pack library (issues #1176, #1177).

Tests run against fakes (no database), following TESTING.md Section 4.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.application.resource_packs import (
    MAX_RESOURCE_PACK_BYTES,
    AssignResourcePack,
    DeleteResourcePack,
    DownloadResourcePack,
    GetResourcePackAssignment,
    ListResourcePacks,
    UnassignResourcePack,
    UploadResourcePack,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    PermissionDeniedError,
    ResourcePackInUseError,
    ResourcePackNotFoundError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackAssignment,
    ResourcePackId,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.fakes import (
    FakeClock,
    FakeFileStore,
    FakeLifecycleLock,
    FakeResourcePackStore,
    FakeServerRepository,
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


# ---------------------------------------------------------------------------
# Assignment use cases (issue #1177)
# ---------------------------------------------------------------------------

_COMMUNITY_ID = CommunityId(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
_BASE_URL = "https://example.com"


def _at_rest_server(
    community_id: CommunityId = _COMMUNITY_ID,
    server_id: ServerId | None = None,
) -> Server:
    sid = server_id or ServerId(uuid.uuid4())
    return Server(
        id=sid,
        community_id=community_id,
        name=ServerName("test-server"),
        mc_edition="java",
        mc_version="1.21",
        server_type=ServerType("vanilla"),
        execution_backend=ExecutionBackend.CONTAINER,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _running_server(
    community_id: CommunityId = _COMMUNITY_ID,
    server_id: ServerId | None = None,
) -> Server:
    from mc_server_dashboard_api.servers.domain.value_objects import WorkerId

    sid = server_id or ServerId(uuid.uuid4())
    return Server(
        id=sid,
        community_id=community_id,
        name=ServerName("running-server"),
        mc_edition="java",
        mc_version="1.21",
        server_type=ServerType("vanilla"),
        execution_backend=ExecutionBackend.CONTAINER,
        config={},
        desired_state=DesiredState.RUNNING,
        observed_state=ObservedState.RUNNING,
        observed_at=_NOW,
        assigned_worker_id=WorkerId(uuid.uuid4()),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _seed_pack(uow: FakeUnitOfWork) -> ResourcePack:
    pack = ResourcePack(
        id=ResourcePackId.new(),
        filename="test-pack.zip",
        display_name="Test Pack",
        description=None,
        sha1_hash="abc123",
        sha256_hash="def456",
        size_bytes=1234,
        uploaded_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )
    uow.resource_packs.packs[pack.id] = pack
    return pack


class TestAssignResourcePack:
    async def test_assign_happy_path(self) -> None:
        server = _at_rest_server()
        servers = FakeServerRepository()
        servers.seed(server)
        uow = FakeUnitOfWork(servers=servers)
        pack = _seed_pack(uow)
        file_store = FakeFileStore()
        file_store.files["server.properties"] = b"motd=hi\n"
        lock = FakeLifecycleLock()
        user_id = uuid.uuid4()

        uc = AssignResourcePack(
            uow=uow,
            file_store=file_store,
            clock=FakeClock(_NOW),
            lifecycle_lock=lock,
        )
        assignment, returned_pack = await uc(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            resource_pack_id=pack.id,
            require_resource_pack=True,
            resource_pack_prompt="Install this",
            assigned_by=user_id,
            public_base_url=_BASE_URL,
        )

        assert assignment.server_id == server.id
        assert assignment.resource_pack_id == pack.id
        assert assignment.require_resource_pack is True
        assert assignment.resource_pack_prompt == "Install this"
        assert assignment.assigned_by == user_id
        assert uow.commits == 1

        # server.properties was rewritten
        props = file_store.files["server.properties"].decode()
        assert f"resource-pack={_BASE_URL}/api/public/resource-packs/" in props
        assert "resource-pack-sha1=abc123" in props
        assert "require-resource-pack=true" in props
        assert "resource-pack-prompt=Install this" in props
        assert "motd=hi" in props

        # Lifecycle lock was held
        assert lock.events == [(server.id, "acquire"), (server.id, "release")]

    async def test_assign_creates_properties_if_missing(self) -> None:
        server = _at_rest_server()
        servers = FakeServerRepository()
        servers.seed(server)
        uow = FakeUnitOfWork(servers=servers)
        pack = _seed_pack(uow)
        file_store = FakeFileStore()  # no server.properties seeded

        uc = AssignResourcePack(
            uow=uow,
            file_store=file_store,
            clock=FakeClock(_NOW),
        )
        await uc(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            resource_pack_id=pack.id,
            require_resource_pack=False,
            resource_pack_prompt=None,
            assigned_by=uuid.uuid4(),
            public_base_url=_BASE_URL,
        )

        assert "server.properties" in file_store.files
        props = file_store.files["server.properties"].decode()
        assert "resource-pack=" in props

    async def test_assign_upserts_existing_assignment(self) -> None:
        server = _at_rest_server()
        servers = FakeServerRepository()
        servers.seed(server)
        uow = FakeUnitOfWork(servers=servers)
        pack1 = _seed_pack(uow)
        pack2 = _seed_pack(uow)
        file_store = FakeFileStore()
        file_store.files["server.properties"] = b"motd=hi\n"

        uc = AssignResourcePack(
            uow=uow,
            file_store=file_store,
            clock=FakeClock(_NOW),
        )
        # First assignment
        await uc(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            resource_pack_id=pack1.id,
            require_resource_pack=False,
            resource_pack_prompt=None,
            assigned_by=uuid.uuid4(),
            public_base_url=_BASE_URL,
        )
        # Second assignment replaces the first
        assignment, _ = await uc(
            community_id=_COMMUNITY_ID,
            server_id=server.id,
            resource_pack_id=pack2.id,
            require_resource_pack=True,
            resource_pack_prompt=None,
            assigned_by=uuid.uuid4(),
            public_base_url=_BASE_URL,
        )

        assert assignment.resource_pack_id == pack2.id
        stored = uow.resource_packs.assignments.get(server.id)
        assert stored is not None
        assert stored.resource_pack_id == pack2.id

    async def test_assign_rejects_unsettled_server(self) -> None:
        server = _running_server()
        servers = FakeServerRepository()
        servers.seed(server)
        uow = FakeUnitOfWork(servers=servers)
        pack = _seed_pack(uow)
        file_store = FakeFileStore()

        uc = AssignResourcePack(
            uow=uow,
            file_store=file_store,
            clock=FakeClock(_NOW),
        )
        with pytest.raises(ServerFilesUnsettledError):
            await uc(
                community_id=_COMMUNITY_ID,
                server_id=server.id,
                resource_pack_id=pack.id,
                require_resource_pack=False,
                resource_pack_prompt=None,
                assigned_by=uuid.uuid4(),
                public_base_url=_BASE_URL,
            )

    async def test_assign_rejects_unknown_server(self) -> None:
        uow = FakeUnitOfWork()
        pack = _seed_pack(uow)
        file_store = FakeFileStore()

        uc = AssignResourcePack(
            uow=uow,
            file_store=file_store,
            clock=FakeClock(_NOW),
        )
        with pytest.raises(ServerNotFoundError):
            await uc(
                community_id=_COMMUNITY_ID,
                server_id=ServerId(uuid.uuid4()),
                resource_pack_id=pack.id,
                require_resource_pack=False,
                resource_pack_prompt=None,
                assigned_by=uuid.uuid4(),
                public_base_url=_BASE_URL,
            )

    async def test_assign_rejects_unknown_pack(self) -> None:
        server = _at_rest_server()
        servers = FakeServerRepository()
        servers.seed(server)
        uow = FakeUnitOfWork(servers=servers)
        file_store = FakeFileStore()

        uc = AssignResourcePack(
            uow=uow,
            file_store=file_store,
            clock=FakeClock(_NOW),
        )
        with pytest.raises(ResourcePackNotFoundError):
            await uc(
                community_id=_COMMUNITY_ID,
                server_id=server.id,
                resource_pack_id=ResourcePackId.new(),
                require_resource_pack=False,
                resource_pack_prompt=None,
                assigned_by=uuid.uuid4(),
                public_base_url=_BASE_URL,
            )


class TestUnassignResourcePack:
    async def test_unassign_happy_path(self) -> None:
        server = _at_rest_server()
        servers = FakeServerRepository()
        servers.seed(server)
        uow = FakeUnitOfWork(servers=servers)
        pack = _seed_pack(uow)
        file_store = FakeFileStore()
        file_store.files["server.properties"] = (
            b"motd=hi\nresource-pack=url\nresource-pack-sha1=sha\n"
            b"require-resource-pack=true\nresource-pack-prompt=Hi\n"
        )
        lock = FakeLifecycleLock()

        # Seed an assignment
        assignment = ResourcePackAssignment(
            server_id=server.id,
            resource_pack_id=pack.id,
            require_resource_pack=True,
            resource_pack_prompt="Hi",
            assigned_by=uuid.uuid4(),
            created_at=_NOW,
            updated_at=_NOW,
        )
        uow.resource_packs.assignments[server.id] = assignment

        uc = UnassignResourcePack(
            uow=uow,
            file_store=file_store,
            lifecycle_lock=lock,
        )
        await uc(community_id=_COMMUNITY_ID, server_id=server.id)

        # Assignment removed
        assert server.id not in uow.resource_packs.assignments
        # server.properties cleared
        props = file_store.files["server.properties"].decode()
        assert "resource-pack=" not in props
        assert "resource-pack-sha1=" not in props
        assert "require-resource-pack=" not in props
        assert "resource-pack-prompt=" not in props
        assert "motd=hi" in props
        # Lock held
        assert lock.events == [(server.id, "acquire"), (server.id, "release")]
        assert uow.commits == 1

    async def test_unassign_rejects_when_no_assignment(self) -> None:
        server = _at_rest_server()
        servers = FakeServerRepository()
        servers.seed(server)
        uow = FakeUnitOfWork(servers=servers)
        file_store = FakeFileStore()

        uc = UnassignResourcePack(uow=uow, file_store=file_store)
        with pytest.raises(ResourcePackNotFoundError):
            await uc(community_id=_COMMUNITY_ID, server_id=server.id)

    async def test_unassign_rejects_unsettled_server(self) -> None:
        server = _running_server()
        servers = FakeServerRepository()
        servers.seed(server)
        uow = FakeUnitOfWork(servers=servers)
        file_store = FakeFileStore()

        uc = UnassignResourcePack(uow=uow, file_store=file_store)
        with pytest.raises(ServerFilesUnsettledError):
            await uc(community_id=_COMMUNITY_ID, server_id=server.id)


class TestGetResourcePackAssignment:
    async def test_get_returns_assignment_and_pack(self) -> None:
        server = _at_rest_server()
        servers = FakeServerRepository()
        servers.seed(server)
        uow = FakeUnitOfWork(servers=servers)
        pack = _seed_pack(uow)

        assignment = ResourcePackAssignment(
            server_id=server.id,
            resource_pack_id=pack.id,
            require_resource_pack=True,
            resource_pack_prompt="Hello",
            assigned_by=uuid.uuid4(),
            created_at=_NOW,
            updated_at=_NOW,
        )
        uow.resource_packs.assignments[server.id] = assignment

        uc = GetResourcePackAssignment(uow=uow)
        result = await uc(community_id=_COMMUNITY_ID, server_id=server.id)

        assert result is not None
        returned_assignment, returned_pack = result
        assert returned_assignment.server_id == server.id
        assert returned_pack.id == pack.id

    async def test_get_returns_none_when_unassigned(self) -> None:
        server = _at_rest_server()
        servers = FakeServerRepository()
        servers.seed(server)
        uow = FakeUnitOfWork(servers=servers)

        uc = GetResourcePackAssignment(uow=uow)
        result = await uc(community_id=_COMMUNITY_ID, server_id=server.id)

        assert result is None

    async def test_get_rejects_unknown_server(self) -> None:
        uow = FakeUnitOfWork()
        uc = GetResourcePackAssignment(uow=uow)

        with pytest.raises(ServerNotFoundError):
            await uc(
                community_id=_COMMUNITY_ID,
                server_id=ServerId(uuid.uuid4()),
            )
