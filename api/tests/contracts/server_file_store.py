"""FileStore seam contract shared by its fake and Storage adapters.

Ordinary file and directory mutation belongs to the Storage Port contract.
These cases cover the servers-only archive seam and string-path translation,
including aliases and the domain-level errors observed by callers.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TypedDict

import pytest

from mc_server_dashboard_api.servers.domain.errors import (
    InvalidFilePathError,
    ServerFileNotFoundError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileStore
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId


class _Scope(TypedDict):
    community_id: CommunityId
    server_id: ServerId


@dataclass(frozen=True)
class FileStoreHarness:
    store: FileStore
    community_id: CommunityId
    server_id: ServerId

    async def write(self, path: str, content: bytes) -> None:
        await self.store.write_file(
            community_id=self.community_id,
            server_id=self.server_id,
            rel_path=path,
            content=content,
        )

    def download(self, path: str) -> AsyncIterator[bytes]:
        return self.store.download_dir(
            community_id=self.community_id, server_id=self.server_id, rel_path=path
        )

    def export(self, path: str, extra: list[tuple[str, bytes]]) -> AsyncIterator[bytes]:
        return self.store.export_dir(
            community_id=self.community_id,
            server_id=self.server_id,
            rel_path=path,
            extra=extra,
        )


async def _archive(stream: AsyncIterator[bytes]) -> dict[str, bytes]:
    content = b"".join([chunk async for chunk in stream])
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class FileStoreContract:
    async def test_directory_download_contains_only_the_named_subtree(
        self, file_store_harness: FileStoreHarness
    ) -> None:
        harness = file_store_harness
        await harness.write("world/level.dat", b"level")
        await harness.write("world/region/r.0.0.mca", b"region")
        await harness.write("outside.txt", b"outside")

        entries = await _archive(harness.download("world"))

        assert entries == {
            "level.dat": b"level",
            "region/r.0.0.mca": b"region",
        }

    async def test_export_appends_extra_entries_after_the_subtree(
        self, file_store_harness: FileStoreHarness
    ) -> None:
        harness = file_store_harness
        await harness.write("world/level.dat", b"level")
        await harness.write("outside.txt", b"outside")

        entries = await _archive(
            harness.export("world", [("export_metadata.json", b"metadata")])
        )

        assert entries == {
            "level.dat": b"level",
            "export_metadata.json": b"metadata",
        }

    @pytest.mark.parametrize("root", ["", ".", "./"])
    async def test_root_aliases_zip_the_working_set(
        self, file_store_harness: FileStoreHarness, root: str
    ) -> None:
        harness = file_store_harness
        await harness.write("seed.txt", b"seed")

        assert await _archive(harness.download(root)) == {"seed.txt": b"seed"}
        assert await _archive(harness.export(root, [])) == {"seed.txt": b"seed"}

    @pytest.mark.parametrize("alias", ["world", "world/", "./world", "world//"])
    async def test_directory_aliases_select_the_same_subtree(
        self, file_store_harness: FileStoreHarness, alias: str
    ) -> None:
        harness = file_store_harness
        await harness.write("world/level.dat", b"level")

        assert await _archive(harness.download(alias)) == {"level.dat": b"level"}
        assert await _archive(harness.export(alias, [])) == {"level.dat": b"level"}

    async def test_created_empty_directory_streams_an_empty_archive(
        self, file_store_harness: FileStoreHarness
    ) -> None:
        harness = file_store_harness
        await harness.write("seed.txt", b"seed")
        await harness.store.make_dir(
            community_id=harness.community_id,
            server_id=harness.server_id,
            rel_path="empty",
        )

        assert await _archive(harness.download("empty")) == {}
        assert await _archive(harness.export("empty", [])) == {}
        assert await _archive(
            harness.export("empty", [("export_metadata.json", b"metadata")])
        ) == {"export_metadata.json": b"metadata"}

    @pytest.mark.parametrize("method", ["download", "export"])
    @pytest.mark.parametrize("path", ["missing", "seed.txt"])
    async def test_missing_directory_is_reported_on_iteration(
        self, file_store_harness: FileStoreHarness, method: str, path: str
    ) -> None:
        harness = file_store_harness
        await harness.write("seed.txt", b"seed")
        stream = (
            harness.download(path) if method == "download" else harness.export(path, [])
        )

        with pytest.raises(ServerFileNotFoundError):
            await _archive(stream)

    @pytest.mark.parametrize(
        "method",
        [
            "validate_rel_path",
            "read_file",
            "open_file_stream",
            "list_dir",
            "path_exists",
            "write_file",
            "delete_file",
            "delete_dir",
            "rename_file_from",
            "rename_file_to",
            "rename_dir_from",
            "rename_dir_to",
            "make_dir",
        ],
    )
    async def test_aliases_resolve_to_the_same_file_or_directory(
        self, file_store_harness: FileStoreHarness, method: str
    ) -> None:
        harness = file_store_harness
        await harness.write("world/level.dat", b"level")
        await harness.write("outside.txt", b"outside")
        store = harness.store
        scope: _Scope = {
            "community_id": harness.community_id,
            "server_id": harness.server_id,
        }
        file_alias = "./world//level.dat/"
        dir_alias = "./world//"

        if method == "validate_rel_path":
            store.validate_rel_path(file_alias)
        elif method == "read_file":
            assert await store.read_file(**scope, rel_path=file_alias) == b"level"
        elif method == "open_file_stream":
            assert (
                b"".join(
                    [
                        chunk
                        async for chunk in store.open_file_stream(
                            **scope, rel_path=file_alias
                        )
                    ]
                )
                == b"level"
            )
        elif method == "list_dir":
            assert {
                entry.name
                for entry in await store.list_dir(**scope, rel_path=dir_alias)
            } == {"level.dat"}
        elif method == "path_exists":
            assert await store.path_exists(**scope, rel_path=file_alias)
        elif method == "write_file":
            await store.write_file(**scope, rel_path=file_alias, content=b"changed")
            assert (
                await store.read_file(**scope, rel_path="world/level.dat") == b"changed"
            )
        elif method == "delete_file":
            await store.delete_file(**scope, rel_path=file_alias)
            assert not await store.path_exists(**scope, rel_path="world/level.dat")
        elif method == "delete_dir":
            await store.delete_dir(**scope, rel_path=dir_alias)
            assert not await store.path_exists(**scope, rel_path="world")
        elif method == "rename_file_from":
            await store.rename_file(**scope, from_path=file_alias, to_path="moved.dat")
            assert await store.read_file(**scope, rel_path="moved.dat") == b"level"
        elif method == "rename_file_to":
            await store.rename_file(
                **scope, from_path="world/level.dat", to_path="./moved.dat/"
            )
            assert await store.read_file(**scope, rel_path="moved.dat") == b"level"
        elif method == "rename_dir_from":
            await store.rename_dir(**scope, from_path=dir_alias, to_path="moved")
            assert (
                await store.read_file(**scope, rel_path="moved/level.dat") == b"level"
            )
        elif method == "rename_dir_to":
            await store.rename_dir(**scope, from_path="world", to_path="./moved//")
            assert (
                await store.read_file(**scope, rel_path="moved/level.dat") == b"level"
            )
        else:
            await store.make_dir(**scope, rel_path="./created//")
            assert await store.path_exists(**scope, rel_path="created")

        assert await store.read_file(**scope, rel_path="outside.txt") == b"outside"

    @pytest.mark.parametrize(
        "method",
        [
            "validate_rel_path",
            "read_file",
            "open_file_stream",
            "list_dir",
            "path_exists",
            "write_file",
            "retain_if_changed",
            "delete_file",
            "delete_dir",
            "rename_file_from",
            "rename_file_to",
            "rename_dir_from",
            "rename_dir_to",
            "make_dir",
            "download_dir",
            "export_dir",
            "list_versions",
            "read_version",
            "rollback",
        ],
    )
    async def test_traversal_is_rejected_at_the_file_store_seam(
        self, file_store_harness: FileStoreHarness, method: str
    ) -> None:
        harness = file_store_harness
        await harness.write("seed.txt", b"seed")
        store = harness.store
        scope: _Scope = {
            "community_id": harness.community_id,
            "server_id": harness.server_id,
        }
        path = "../escape"

        with pytest.raises(InvalidFilePathError):
            if method == "validate_rel_path":
                store.validate_rel_path(path)
            elif method == "open_file_stream":
                async for _ in store.open_file_stream(**scope, rel_path=path):
                    pass
            elif method == "download_dir":
                await _archive(harness.download(path))
            elif method == "export_dir":
                await _archive(harness.export(path, []))
            elif method == "rename_file_from":
                await store.rename_file(**scope, from_path=path, to_path="moved.txt")
            elif method == "rename_file_to":
                await store.rename_file(**scope, from_path="seed.txt", to_path=path)
            elif method == "rename_dir_from":
                await store.rename_dir(**scope, from_path=path, to_path="moved")
            elif method == "rename_dir_to":
                await store.rename_dir(**scope, from_path="missing", to_path=path)
            elif method == "write_file":
                await store.write_file(**scope, rel_path=path, content=b"new")
            elif method in {"read_version", "rollback"}:
                await getattr(store, method)(**scope, rel_path=path, version_id="v1")
            else:
                await getattr(store, method)(**scope, rel_path=path)
