"""Unit tests for the keyless Modrinth catalog adapter (issue #1264).

No live network ever (TESTING.md Section 4): a fake :class:`CatalogHttpClient`
serves recorded Modrinth payloads keyed by (url, params) and records calls.
Covers facet mapping, project detail, the client/server side mapping, and
download.
"""

from __future__ import annotations

import json

import pytest

from mc_server_dashboard_api.servers.adapters.modrinth_catalog import (
    ModrinthCatalogProvider,
)
from mc_server_dashboard_api.servers.domain.catalog_http import (
    CatalogHttpClient,
    CatalogHttpError,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogProjectNotFoundError,
    CatalogUnavailableError,
)

_BASE = "https://api.modrinth.com/v2"


class FakeHttpClient(CatalogHttpClient):
    """Serve recorded JSON / bytes by URL; record calls; optionally fail."""

    def __init__(
        self,
        json_payloads: dict[str, object] | None = None,
        byte_payloads: dict[str, bytes] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self._json = json_payloads or {}
        self._bytes = byte_payloads or {}
        self.fail = fail
        self.json_calls: list[tuple[str, dict[str, str] | None]] = []
        self.byte_calls: list[str] = []

    async def get_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> object:
        self.json_calls.append((url, params))
        if self.fail:
            raise CatalogHttpError(f"forced failure for {url}")
        if url not in self._json:
            raise CatalogHttpError(f"no fixture for {url}")
        return self._json[url]

    async def get_bytes(self, url: str) -> bytes:
        self.byte_calls.append(url)
        if self.fail:
            raise CatalogHttpError(f"forced failure for {url}")
        if url not in self._bytes:
            raise CatalogHttpError(f"no fixture for {url}")
        return self._bytes[url]


def _provider(client: FakeHttpClient) -> ModrinthCatalogProvider:
    return ModrinthCatalogProvider(http=client)


def _as_file_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


class TestSearch:
    async def test_search_maps_hits(self) -> None:
        payload = {
            "hits": [
                {
                    "project_id": "AABBCCDD",
                    "slug": "sodium",
                    "title": "Sodium",
                    "description": "A modern rendering engine",
                    "project_type": "mod",
                    "client_side": "required",
                    "server_side": "unsupported",
                    "loaders": ["fabric"],
                    "versions": ["1.20.1", "1.20.4"],
                    "downloads": 5_000_000,
                    "icon_url": "https://cdn.modrinth.com/icon.png",
                }
            ],
            "total_hits": 1,
            "offset": 0,
            "limit": 20,
        }
        client = FakeHttpClient({f"{_BASE}/search": payload})
        result = await _provider(client).search(query="sodium")

        assert result.total == 1
        hit = result.hits[0]
        assert hit.project_id == "AABBCCDD"
        assert hit.slug == "sodium"
        assert hit.title == "Sodium"
        assert hit.project_type == "mod"
        assert hit.loaders == ["fabric"]
        assert hit.downloads == 5_000_000
        assert hit.icon_url == "https://cdn.modrinth.com/icon.png"
        # client required + server unsupported -> client-only.
        assert hit.side == "client"

    async def test_search_passes_query_and_pagination(self) -> None:
        client = FakeHttpClient({f"{_BASE}/search": {"hits": [], "total_hits": 0}})
        await _provider(client).search(query="map", limit=5, offset=10)

        url, params = client.json_calls[0]
        assert url == f"{_BASE}/search"
        assert params is not None
        assert params["query"] == "map"
        assert params["limit"] == "5"
        assert params["offset"] == "10"
        # With no loader/version facets, search is still constrained to mods so
        # modpacks/resource packs/shaders are excluded.
        facets = json.loads(params["facets"])
        assert facets == [["project_type:mod"]]

    async def test_search_builds_loader_and_version_facets(self) -> None:
        client = FakeHttpClient({f"{_BASE}/search": {"hits": [], "total_hits": 0}})
        await _provider(client).search(
            query="x", loader="fabric", game_version="1.20.4"
        )

        _url, params = client.json_calls[0]
        assert params is not None
        facets = json.loads(params["facets"])
        # AND of the loader facet, the version facet, and project_type:mod.
        assert ["categories:fabric"] in facets
        assert ["versions:1.20.4"] in facets
        assert ["project_type:mod"] in facets

    async def test_search_only_loader_facet(self) -> None:
        client = FakeHttpClient({f"{_BASE}/search": {"hits": [], "total_hits": 0}})
        await _provider(client).search(query="x", loader="forge")

        _url, params = client.json_calls[0]
        assert params is not None
        facets = json.loads(params["facets"])
        assert ["categories:forge"] in facets
        assert not any(f[0].startswith("versions:") for f in facets)

    async def test_search_source_failure_raises_unavailable(self) -> None:
        client = FakeHttpClient(fail=True)
        with pytest.raises(CatalogUnavailableError):
            await _provider(client).search(query="x")


def _project_payload(
    *, client_side: str, server_side: str, project_id: str = "AABBCCDD"
) -> dict[str, object]:
    return {
        "id": project_id,
        "slug": "sodium",
        "title": "Sodium",
        "description": "A modern rendering engine",
        "project_type": "mod",
        "client_side": client_side,
        "server_side": server_side,
        "loaders": ["fabric"],
        "game_versions": ["1.20.4"],
        "versions": ["VER111"],
    }


def _version_payload(*, version_id: str = "VER111") -> dict[str, object]:
    return {
        "id": version_id,
        "project_id": "AABBCCDD",
        "name": "Sodium 0.5.3",
        "version_number": "0.5.3",
        "loaders": ["fabric"],
        "game_versions": ["1.20.4"],
        "dependencies": [
            {
                "version_id": None,
                "project_id": "FABRICAPI",
                "dependency_type": "required",
                "file_name": None,
            }
        ],
        "files": [
            {
                "hashes": {"sha1": "aaa", "sha512": "deadbeef512"},
                "url": "https://cdn.modrinth.com/data/AABBCCDD/sodium.jar",
                "filename": "sodium-fabric-0.5.3.jar",
                "primary": True,
                "size": 1234,
            }
        ],
    }


class TestGetProject:
    async def test_get_project_maps_detail_and_versions(self) -> None:
        project = _project_payload(client_side="required", server_side="optional")
        client = FakeHttpClient(
            {
                f"{_BASE}/project/sodium": project,
                f"{_BASE}/project/sodium/version": [_version_payload()],
            }
        )
        result = await _provider(client).get_project("sodium")

        assert result.project_id == "AABBCCDD"
        assert result.slug == "sodium"
        assert result.title == "Sodium"
        assert result.project_type == "mod"
        assert result.loaders == ["fabric"]
        assert result.game_versions == ["1.20.4"]
        # client required + server optional -> needed on both.
        assert result.side == "both"

        assert len(result.versions) == 1
        ver = result.versions[0]
        assert ver.version_id == "VER111"
        assert ver.version_number == "0.5.3"
        assert ver.filename == "sodium-fabric-0.5.3.jar"
        assert ver.download_url.endswith("sodium.jar")
        assert ver.sha512 == "deadbeef512"
        assert ver.loaders == ["fabric"]
        # Catalog dependency captured.
        assert len(ver.dependencies) == 1
        assert ver.dependencies[0].project_id == "FABRICAPI"
        assert ver.dependencies[0].dependency_type == "required"

    async def test_get_project_not_found_maps_to_not_found(self) -> None:
        """A 404 status from the client surfaces as not-found, not unavailable."""

        class NotFoundClient(FakeHttpClient):
            async def get_json(
                self, url: str, *, params: dict[str, str] | None = None
            ) -> object:
                raise CatalogHttpError("404 Not Found", status=404)

        with pytest.raises(CatalogProjectNotFoundError):
            await _provider(NotFoundClient()).get_project("missing")

    async def test_get_project_server_error_maps_to_unavailable(self) -> None:
        """A 5xx (or transport failure) surfaces as unavailable, not not-found."""

        class ServerErrorClient(FakeHttpClient):
            async def get_json(
                self, url: str, *, params: dict[str, str] | None = None
            ) -> object:
                raise CatalogHttpError("503", status=503)

        with pytest.raises(CatalogUnavailableError):
            await _provider(ServerErrorClient()).get_project("x")

    async def test_get_project_picks_primary_file(self) -> None:
        project = _project_payload(client_side="optional", server_side="required")
        version = _version_payload()
        # Add a non-primary file before the primary one to prove selection.
        secondary = {
            "hashes": {"sha512": "secondary"},
            "url": "https://cdn.modrinth.com/sources.jar",
            "filename": "sodium-sources.jar",
            "primary": False,
            "size": 99,
        }
        version["files"] = [secondary, *_as_file_list(version["files"])]
        client = FakeHttpClient(
            {
                f"{_BASE}/project/sodium": project,
                f"{_BASE}/project/sodium/version": [version],
            }
        )
        result = await _provider(client).get_project("sodium")
        ver = result.versions[0]
        assert ver.filename == "sodium-fabric-0.5.3.jar"
        assert ver.sha512 == "deadbeef512"
        # client optional + server required -> both.
        assert result.side == "both"


class TestSideMapping:
    @pytest.mark.parametrize(
        ("client_side", "server_side", "expected"),
        [
            ("required", "unsupported", "client"),
            ("optional", "unsupported", "client"),
            ("unsupported", "required", "server"),
            ("unsupported", "optional", "server"),
            ("required", "required", "both"),
            ("optional", "optional", "both"),
            ("required", "optional", "both"),
            # Both unknown / unsupported -> safe default (present everywhere).
            ("unknown", "unknown", "both"),
            ("unsupported", "unsupported", "both"),
        ],
    )
    async def test_side_mapping(
        self, client_side: str, server_side: str, expected: str
    ) -> None:
        project = _project_payload(client_side=client_side, server_side=server_side)
        client = FakeHttpClient(
            {
                f"{_BASE}/project/x": project,
                f"{_BASE}/project/x/version": [],
            }
        )
        result = await _provider(client).get_project("x")
        assert result.side == expected


class TestGetVersion:
    async def test_get_version_maps_file(self) -> None:
        client = FakeHttpClient({f"{_BASE}/version/VER111": _version_payload()})
        ver = await _provider(client).get_version("VER111")
        assert ver.version_id == "VER111"
        assert ver.download_url.endswith("sodium.jar")
        assert ver.sha512 == "deadbeef512"

    async def test_get_version_source_failure(self) -> None:
        client = FakeHttpClient(fail=True)
        with pytest.raises(CatalogUnavailableError):
            await _provider(client).get_version("VER111")


class TestDownload:
    async def test_download_returns_bytes(self) -> None:
        url = "https://cdn.modrinth.com/data/AABBCCDD/sodium.jar"
        client = FakeHttpClient(byte_payloads={url: b"jar-bytes"})
        data = await _provider(client).download(url)
        assert data == b"jar-bytes"
        assert client.byte_calls == [url]

    async def test_download_failure(self) -> None:
        client = FakeHttpClient(fail=True)
        with pytest.raises(CatalogUnavailableError):
            await _provider(client).download("https://x/y.jar")
