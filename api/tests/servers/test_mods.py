"""Use-case tests for the mod library (issue #1261).

Tests run against fakes (no database), following TESTING.md Section 4.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import uuid
import zipfile

import pytest

from mc_server_dashboard_api.servers.application.mods import (
    MAX_MOD_BYTES,
    DeleteMod,
    DownloadMod,
    ListMods,
    UploadMod,
)
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidModJarError,
    ModInUseError,
    ModNotFoundError,
    PermissionDeniedError,
)
from mc_server_dashboard_api.servers.domain.mod import ModId
from mc_server_dashboard_api.servers.domain.server_mod import (
    ServerModAssignment,
    ServerModId,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from tests.servers.fakes import FakeClock, FakeModStore, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 16, 12, 0, 0, tzinfo=dt.timezone.utc)


def _make_jar(entries: dict[str, str | bytes]) -> bytes:
    """Build a jar (zip) in memory from {path: content} pairs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


_FABRIC_MANIFEST = {
    "id": "examplemod",
    "version": "1.2.3",
    "depends": {"minecraft": "~1.20.4", "fabric-api": "*"},
    "provides": ["examplemod-api"],
    "environment": "client",
}
_JAR_CONTENT = _make_jar({"fabric.mod.json": json.dumps(_FABRIC_MANIFEST)})


def _make_upload(
    uow: FakeUnitOfWork | None = None,
    store: FakeModStore | None = None,
    clock: FakeClock | None = None,
) -> UploadMod:
    return UploadMod(
        uow=uow or FakeUnitOfWork(),
        store=store or FakeModStore(),
        clock=clock or FakeClock(_NOW),
    )


class TestUploadMod:
    async def test_upload_happy_path(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeModStore()
        uc = _make_upload(uow=uow, store=store)
        user_id = uuid.uuid4()

        mod = await uc(
            filename="my-mod.jar",
            display_name="My Mod",
            content=_JAR_CONTENT,
            uploaded_by=user_id,
        )

        assert mod.filename == "my-mod.jar"
        assert mod.display_name == "My Mod"
        assert mod.size_bytes == len(_JAR_CONTENT)
        assert mod.uploaded_by == user_id
        assert mod.sha256_hash == hashlib.sha256(_JAR_CONTENT).hexdigest()
        assert mod.sha512_hash == hashlib.sha512(_JAR_CONTENT).hexdigest()
        assert mod.source == "local"
        assert mod.created_at == _NOW
        # Blob stored
        assert mod.id in store.blobs
        # DB row committed
        assert uow.commits == 1
        assert mod.id in uow.mods.mods

    async def test_upload_parses_manifest_into_stored_fields(self) -> None:
        uc = _make_upload()
        mod = await uc(
            filename="examplemod.jar",
            display_name="Example",
            content=_JAR_CONTENT,
            uploaded_by=uuid.uuid4(),
        )

        assert mod.loader_type == "fabric"
        assert mod.mod_identifier == "examplemod"
        assert mod.version_number == "1.2.3"
        assert mod.provides == ["examplemod-api"]
        assert mod.mc_versions == ["~1.20.4"]
        assert any(dep["mod_identifier"] == "fabric-api" for dep in mod.dependencies)
        # environment=client -> detected side
        assert mod.side == "client"

    async def test_upload_dedup_returns_existing(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeModStore()
        uc = _make_upload(uow=uow, store=store)
        user_id = uuid.uuid4()

        first = await uc(
            filename="dup.jar",
            display_name="First",
            content=_JAR_CONTENT,
            uploaded_by=user_id,
        )
        second = await uc(
            filename="dup-renamed.jar",
            display_name="Second",
            content=_JAR_CONTENT,
            uploaded_by=uuid.uuid4(),
        )

        # The second upload resolves to the existing entry, no duplicate stored.
        assert second.id == first.id
        assert second.display_name == "First"
        assert len(uow.mods.mods) == 1
        assert len(store.blobs) == 1
        # Only the first upload committed a row.
        assert uow.commits == 1

    async def test_upload_side_override(self) -> None:
        uc = _make_upload()
        # Manifest detects client; caller overrides to both.
        mod = await uc(
            filename="override.jar",
            display_name="Override",
            content=_JAR_CONTENT,
            uploaded_by=uuid.uuid4(),
            side="both",
        )
        assert mod.side == "both"

    async def test_upload_defaults_side_to_detected(self) -> None:
        uc = _make_upload()
        mod = await uc(
            filename="detected.jar",
            display_name="Detected",
            content=_JAR_CONTENT,
            uploaded_by=uuid.uuid4(),
            side=None,
        )
        # No override -> parser's auto-detected value (client here).
        assert mod.side == "client"

    async def test_upload_rejects_non_jar(self) -> None:
        uc = _make_upload()
        with pytest.raises(ValueError, match="jar"):
            await uc(
                filename="my-mod.zip",
                display_name="Bad",
                content=_JAR_CONTENT,
                uploaded_by=uuid.uuid4(),
            )

    async def test_upload_rejects_oversized(self) -> None:
        uc = _make_upload()
        big = b"\x00" * (MAX_MOD_BYTES + 1)
        with pytest.raises(FileTooLargeError):
            await uc(
                filename="big.jar",
                display_name="Too Big",
                content=big,
                uploaded_by=uuid.uuid4(),
            )

    async def test_upload_rejects_invalid_jar(self) -> None:
        """Not-a-zip content raises InvalidModJarError from the parser."""
        uc = _make_upload()
        with pytest.raises(InvalidModJarError):
            await uc(
                filename="bad.jar",
                display_name="Bad",
                content=b"this is not a jar",
                uploaded_by=uuid.uuid4(),
            )

    async def test_upload_rejects_unrecognized_manifest(self) -> None:
        """A readable jar with no recognized manifest is rejected, not stored.

        Its loader is undeterminable, so it can't be deployed and the DB CHECK
        would reject a "unknown" loader_type. The guard fires before the blob is
        stored, so nothing is orphaned.
        """
        uow = FakeUnitOfWork()
        store = FakeModStore()
        uc = _make_upload(uow=uow, store=store)
        jar = _make_jar({"README.txt": "no manifest here"})
        with pytest.raises(InvalidModJarError):
            await uc(
                filename="plain.jar",
                display_name="Plain",
                content=jar,
                uploaded_by=uuid.uuid4(),
            )
        # Nothing stored or committed.
        assert store.blobs == {}
        assert uow.commits == 0


class TestListMods:
    async def test_list_empty(self) -> None:
        uow = FakeUnitOfWork()
        uc = ListMods(uow=uow)
        assert await uc() == []

    async def test_list_returns_all_ordered(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeModStore()
        upload_uc = _make_upload(uow=uow, store=store)
        user_id = uuid.uuid4()

        await upload_uc(
            filename="b.jar",
            display_name="Beta",
            content=_make_jar({"fabric.mod.json": json.dumps({"id": "b"})}),
            uploaded_by=user_id,
        )
        await upload_uc(
            filename="a.jar",
            display_name="Alpha",
            content=_make_jar({"fabric.mod.json": json.dumps({"id": "a"})}),
            uploaded_by=user_id,
        )

        mods = await ListMods(uow=uow)()
        assert [m.display_name for m in mods] == ["Alpha", "Beta"]

    async def test_list_filters_by_loader(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeModStore()
        upload_uc = _make_upload(uow=uow, store=store)

        await upload_uc(
            filename="fab.jar",
            display_name="Fabric Mod",
            content=_make_jar({"fabric.mod.json": json.dumps({"id": "fab"})}),
            uploaded_by=uuid.uuid4(),
        )
        await upload_uc(
            filename="paper.jar",
            display_name="Paper Plugin",
            content=_make_jar({"plugin.yml": "name: PaperPlugin\nversion: 1.0\n"}),
            uploaded_by=uuid.uuid4(),
        )

        fabric = await ListMods(uow=uow)(loader_type="fabric")
        assert [m.display_name for m in fabric] == ["Fabric Mod"]

    async def test_list_filters_by_side_and_mc(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeModStore()
        upload_uc = _make_upload(uow=uow, store=store)

        await upload_uc(
            filename="client.jar",
            display_name="Client Mod",
            content=_JAR_CONTENT,  # environment=client, mc ~1.20.4
            uploaded_by=uuid.uuid4(),
        )

        assert len(await ListMods(uow=uow)(side="client")) == 1
        assert len(await ListMods(uow=uow)(side="server")) == 0
        assert len(await ListMods(uow=uow)(mc_version="~1.20.4")) == 1
        assert len(await ListMods(uow=uow)(mc_version="1.99")) == 0


class TestDeleteMod:
    async def test_delete_by_uploader(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeModStore()
        upload_uc = _make_upload(uow=uow, store=store)
        user_id = uuid.uuid4()

        mod = await upload_uc(
            filename="del.jar",
            display_name="Delete Me",
            content=_JAR_CONTENT,
            uploaded_by=user_id,
        )

        await DeleteMod(uow=uow, store=store)(
            mod_id=mod.id,
            caller_id=user_id,
            is_platform_admin=False,
        )

        assert mod.id not in uow.mods.mods
        assert mod.id not in store.blobs

    async def test_delete_by_admin(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeModStore()
        upload_uc = _make_upload(uow=uow, store=store)

        mod = await upload_uc(
            filename="del.jar",
            display_name="Delete Me",
            content=_JAR_CONTENT,
            uploaded_by=uuid.uuid4(),
        )

        await DeleteMod(uow=uow, store=store)(
            mod_id=mod.id,
            caller_id=uuid.uuid4(),
            is_platform_admin=True,
        )

        assert mod.id not in uow.mods.mods

    async def test_delete_denied_for_non_uploader(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeModStore()
        upload_uc = _make_upload(uow=uow, store=store)

        mod = await upload_uc(
            filename="del.jar",
            display_name="Mine",
            content=_JAR_CONTENT,
            uploaded_by=uuid.uuid4(),
        )

        with pytest.raises(PermissionDeniedError):
            await DeleteMod(uow=uow, store=store)(
                mod_id=mod.id,
                caller_id=uuid.uuid4(),
                is_platform_admin=False,
            )

    async def test_delete_not_found(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeModStore()
        with pytest.raises(ModNotFoundError):
            await DeleteMod(uow=uow, store=store)(
                mod_id=ModId.new(),
                caller_id=uuid.uuid4(),
                is_platform_admin=False,
            )

    async def test_delete_blocked_while_assigned(self) -> None:
        """A library mod assigned to any server cannot be deleted (issue #1262)."""
        uow = FakeUnitOfWork()
        store = FakeModStore()
        upload_uc = _make_upload(uow=uow, store=store)
        user_id = uuid.uuid4()

        mod = await upload_uc(
            filename="assigned.jar",
            display_name="Assigned",
            content=_JAR_CONTENT,
            uploaded_by=user_id,
        )

        # Assign the mod to a server.
        await uow.mods.add_assignment(
            ServerModAssignment(
                id=ServerModId.new(),
                server_id=ServerId(uuid.uuid4()),
                mod_id=mod.id,
                enabled=True,
                assigned_by=user_id,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )

        with pytest.raises(ModInUseError):
            await DeleteMod(uow=uow, store=store)(
                mod_id=mod.id,
                caller_id=user_id,
                is_platform_admin=False,
            )
        # Blob and row survive the refused delete.
        assert mod.id in uow.mods.mods
        assert mod.id in store.blobs


class TestDownloadMod:
    async def test_download_happy_path(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeModStore()
        upload_uc = _make_upload(uow=uow, store=store)

        mod = await upload_uc(
            filename="dl.jar",
            display_name="Download Me",
            content=_JAR_CONTENT,
            uploaded_by=uuid.uuid4(),
        )

        stream, returned = await DownloadMod(uow=uow, store=store)(mod_id=mod.id)
        chunks = [chunk async for chunk in stream]
        assert b"".join(chunks) == _JAR_CONTENT
        assert returned.filename == "dl.jar"

    async def test_download_not_found(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeModStore()
        with pytest.raises(ModNotFoundError):
            await DownloadMod(uow=uow, store=store)(mod_id=ModId.new())
