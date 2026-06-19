"""Use-case tests for Modrinth import into the library (issue #1264).

Tests run against fakes (no database, no network): a fake
:class:`CatalogProvider` returns recorded version metadata and jar bytes, and the
in-memory mod store/uow capture the persisted entry. Covers the download ->
manifest re-parse -> dedup -> source-field persistence path, the Modrinth-side
override, and integrity (published sha512 mismatch).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import uuid
import zipfile

import pytest

from mc_server_dashboard_api.servers.application.mods import ImportMod
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogDependency,
    CatalogProjectNotFoundError,
    CatalogVersion,
)
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidModJarError,
    ModIntegrityError,
)
from tests.servers.fakes import (
    FakeCatalogProvider,
    FakeClock,
    FakeModStore,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 16, 12, 0, 0, tzinfo=dt.timezone.utc)


def _make_jar(entries: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


_FABRIC_MANIFEST = {
    "id": "sodium",
    "version": "0.5.3",
    "depends": {"minecraft": "~1.20.4", "fabric-api": "*"},
    "environment": "client",
}
_JAR = _make_jar({"fabric.mod.json": json.dumps(_FABRIC_MANIFEST)})
_SHA512 = hashlib.sha512(_JAR).hexdigest()
_DOWNLOAD_URL = "https://cdn.modrinth.com/data/AABBCCDD/sodium.jar"


def _version(
    *,
    version_id: str = "VER111",
    project_id: str = "AABBCCDD",
    filename: str = "sodium-0.5.3.jar",
    sha512: str | None = _SHA512,
    download_url: str = _DOWNLOAD_URL,
) -> CatalogVersion:
    return CatalogVersion(
        version_id=version_id,
        project_id=project_id,
        name="Sodium 0.5.3",
        version_number="0.5.3",
        filename=filename,
        download_url=download_url,
        sha512=sha512,
        loaders=["fabric"],
        game_versions=["1.20.4"],
        dependencies=[
            CatalogDependency(
                project_id="FABRICAPI", version_id=None, dependency_type="required"
            )
        ],
    )


def _make_import(
    *,
    uow: FakeUnitOfWork | None = None,
    store: FakeModStore | None = None,
    provider: FakeCatalogProvider | None = None,
) -> ImportMod:
    return ImportMod(
        uow=uow or FakeUnitOfWork(),
        store=store or FakeModStore(),
        clock=FakeClock(_NOW),
        catalog=provider or FakeCatalogProvider(),
    )


class TestImportMod:
    async def test_import_happy_path_persists_source_fields(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeModStore()
        provider = FakeCatalogProvider(
            versions={"VER111": _version()}, blobs={_DOWNLOAD_URL: _JAR}
        )
        uc = _make_import(uow=uow, store=store, provider=provider)
        user_id = uuid.uuid4()

        mod = await uc(
            project_id="AABBCCDD",
            version_id="VER111",
            side="both",
            imported_by=user_id,
        )

        assert mod.source == "modrinth"
        assert mod.source_project_id == "AABBCCDD"
        assert mod.source_version_id == "VER111"
        # Modrinth-published sha512 persisted.
        assert mod.sha512_hash == _SHA512
        # sha256 computed from the downloaded bytes.
        assert mod.sha256_hash == hashlib.sha256(_JAR).hexdigest()
        assert mod.filename == "sodium-0.5.3.jar"
        assert mod.uploaded_by == user_id
        assert mod.created_at == _NOW
        assert mod.size_bytes == len(_JAR)
        # Blob stored + row committed.
        assert mod.id in store.blobs
        assert uow.commits == 1
        assert mod.id in uow.mods.mods

    async def test_import_reparses_manifest(self) -> None:
        provider = FakeCatalogProvider(
            versions={"VER111": _version()}, blobs={_DOWNLOAD_URL: _JAR}
        )
        uc = _make_import(provider=provider)

        mod = await uc(
            project_id="AABBCCDD",
            version_id="VER111",
            imported_by=uuid.uuid4(),
        )
        # Manifest is the uniform source for loader / mod id / deps.
        assert mod.loader_type == "fabric"
        assert mod.mod_identifier == "sodium"
        assert mod.version_number == "0.5.3"
        assert any(d["mod_identifier"] == "fabric-api" for d in mod.dependencies)

    async def test_import_side_from_modrinth_overrides_manifest(self) -> None:
        # The manifest says environment=client; Modrinth side says both -> both.
        provider = FakeCatalogProvider(
            versions={"VER111": _version()}, blobs={_DOWNLOAD_URL: _JAR}
        )
        uc = _make_import(provider=provider)
        mod = await uc(
            project_id="AABBCCDD",
            version_id="VER111",
            side="both",
            imported_by=uuid.uuid4(),
        )
        assert mod.side == "both"

    async def test_import_defaults_side_to_manifest_when_not_given(self) -> None:
        provider = FakeCatalogProvider(
            versions={"VER111": _version()}, blobs={_DOWNLOAD_URL: _JAR}
        )
        uc = _make_import(provider=provider)
        mod = await uc(
            project_id="AABBCCDD",
            version_id="VER111",
            side=None,
            imported_by=uuid.uuid4(),
        )
        # No Modrinth side passed -> parser's auto-detected value (client here).
        assert mod.side == "client"

    async def test_import_dedup_returns_existing(self) -> None:
        uow = FakeUnitOfWork()
        store = FakeModStore()
        provider = FakeCatalogProvider(
            versions={"VER111": _version()}, blobs={_DOWNLOAD_URL: _JAR}
        )
        uc = _make_import(uow=uow, store=store, provider=provider)

        first = await uc(
            project_id="AABBCCDD",
            version_id="VER111",
            imported_by=uuid.uuid4(),
        )
        second = await uc(
            project_id="AABBCCDD",
            version_id="VER111",
            imported_by=uuid.uuid4(),
        )
        assert second.id == first.id
        assert len(uow.mods.mods) == 1
        assert len(store.blobs) == 1
        assert uow.commits == 1

    async def test_import_version_not_found(self) -> None:
        provider = FakeCatalogProvider(versions={})
        uc = _make_import(provider=provider)
        with pytest.raises(CatalogProjectNotFoundError):
            await uc(
                project_id="AABBCCDD",
                version_id="MISSING",
                imported_by=uuid.uuid4(),
            )

    async def test_import_rejects_oversized(self) -> None:
        big = b"\x00" * (256 * 1024 * 1024 + 1)
        provider = FakeCatalogProvider(
            versions={"VER111": _version(sha512=None)},
            blobs={_DOWNLOAD_URL: big},
        )
        uc = _make_import(provider=provider)
        with pytest.raises(FileTooLargeError):
            await uc(
                project_id="AABBCCDD",
                version_id="VER111",
                imported_by=uuid.uuid4(),
            )

    async def test_import_rejects_non_jar_filename(self) -> None:
        provider = FakeCatalogProvider(
            versions={"VER111": _version(filename="sodium.zip", sha512=None)},
            blobs={_DOWNLOAD_URL: _JAR},
        )
        uc = _make_import(provider=provider)
        with pytest.raises(ValueError, match="jar"):
            await uc(
                project_id="AABBCCDD",
                version_id="VER111",
                imported_by=uuid.uuid4(),
            )

    async def test_import_rejects_unrecognized_manifest(self) -> None:
        plain = _make_jar({"README.txt": "no manifest"})
        provider = FakeCatalogProvider(
            versions={"VER111": _version(sha512=None)},
            blobs={_DOWNLOAD_URL: plain},
        )
        uc = _make_import(provider=provider)
        with pytest.raises(InvalidModJarError):
            await uc(
                project_id="AABBCCDD",
                version_id="VER111",
                imported_by=uuid.uuid4(),
            )

    async def test_import_published_sha512_mismatch_is_integrity_error(self) -> None:
        provider = FakeCatalogProvider(
            versions={"VER111": _version(sha512="0" * 128)},
            blobs={_DOWNLOAD_URL: _JAR},
        )
        uc = _make_import(provider=provider)
        with pytest.raises(ModIntegrityError):
            await uc(
                project_id="AABBCCDD",
                version_id="VER111",
                imported_by=uuid.uuid4(),
            )

    async def test_import_computes_sha512_when_unpublished(self) -> None:
        # Modrinth always publishes sha512, but be robust: compute it when absent.
        provider = FakeCatalogProvider(
            versions={"VER111": _version(sha512=None)}, blobs={_DOWNLOAD_URL: _JAR}
        )
        uc = _make_import(provider=provider)
        mod = await uc(
            project_id="AABBCCDD",
            version_id="VER111",
            imported_by=uuid.uuid4(),
        )
        assert mod.sha512_hash == _SHA512
