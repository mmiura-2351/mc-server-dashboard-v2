"""Adapter tests for :class:`GeyserMcCatalog` (issue #1905).

Covers the GeyserMC-specific behavior — routing predicates, single-artifact
search matching, latest-build parsing into a sha256-carrying catalog file, and
the download-host SSRF guards — without a live network by stubbing ``_get_json``
and exercising the pure paths directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx2
import pytest

from mc_server_dashboard_api.servers.adapters import geysermc_catalog
from mc_server_dashboard_api.servers.adapters.geysermc_catalog import (
    _ALLOWED_DOWNLOAD_HOSTS,
    GeyserMcCatalog,
    _assert_no_private_ips,
)
from mc_server_dashboard_api.servers.domain.errors import (
    CatalogProjectNotFoundError,
    CatalogUnavailableError,
)

_LATEST_URL = (
    "https://download.geysermc.org/v2/projects/floodgate/versions/latest/builds/latest"
)
_CONCRETE_PATH = "/v2/projects/floodgate/versions/2.2.5/builds/138"
_CONCRETE_URL = "https://download.geysermc.org" + _CONCRETE_PATH


@contextmanager
def _mock_transport(
    handler: "Any",
) -> Iterator[None]:
    """Inject an httpx2 ``MockTransport`` into every ``AsyncClient`` in scope.

    Mirrors the ``ModrinthCatalog`` adapter tests: the adapter builds its own
    client, so the transport is patched in via ``__init__`` for the duration.
    """

    transport = httpx2.MockTransport(handler)
    real_init = httpx2.AsyncClient.__init__

    def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        real_init(self_client, **kwargs)

    httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
    try:
        yield
    finally:
        httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]


_SPIGOT_SHA256 = "44bdb908e2fb4ff1b974d5313d048a625a21555a9844cfb86256a98e8e1c6bd1"

# A trimmed real ``.../versions/latest/builds/latest`` response.
_BUILD: dict[str, Any] = {
    "project_id": "floodgate",
    "project_name": "Floodgate",
    "version": "2.2.5",
    "build": 138,
    "time": "2026-06-29T18:24:08.000Z",
    "channel": "default",
    "promoted": False,
    "changes": [],
    "downloads": {
        "bungee": {"name": "floodgate-bungee.jar", "sha256": "aa"},
        "spigot": {
            "name": "floodgate-spigot.jar",
            "sha256": _SPIGOT_SHA256,
        },
        "velocity": {"name": "floodgate-velocity.jar", "sha256": "cc"},
    },
}


def _catalog_with_build(build: dict[str, Any]) -> GeyserMcCatalog:
    catalog = GeyserMcCatalog()

    async def _fake_get_json(path: str) -> Any:
        return build

    catalog._get_json = _fake_get_json  # type: ignore[method-assign]
    return catalog


# -- routing predicates --


def test_handles_only_synthetic_id() -> None:
    catalog = GeyserMcCatalog()
    assert catalog.handles("geysermc-floodgate") is True
    assert catalog.handles("floodgate") is False  # bare slug belongs to Modrinth
    assert catalog.handles("geyser") is False
    assert catalog.handles("fabric-api") is False


def test_handles_url_only_geysermc_host() -> None:
    catalog = GeyserMcCatalog()
    assert (
        catalog.handles_url(
            "https://download.geysermc.org/v2/projects/floodgate/x/downloads/spigot"
        )
        is True
    )
    assert catalog.handles_url("https://cdn.modrinth.com/data/x.jar") is False


# -- search --


@pytest.mark.parametrize("query", ["", "flood", "floodgate", "FLOOD", "gate"])
async def test_search_surfaces_floodgate_for_paper(query: str) -> None:
    catalog = GeyserMcCatalog()
    resp = await catalog.search(query=query, loader="paper", game_versions=["1.21.1"])
    assert [h.project_id for h in resp.hits] == ["geysermc-floodgate"]
    assert resp.total_hits == 1


async def test_search_empty_when_query_does_not_match() -> None:
    catalog = GeyserMcCatalog()
    resp = await catalog.search(query="sodium", loader="paper", game_versions=[])
    assert resp.hits == []
    assert resp.total_hits == 0


async def test_search_empty_for_non_paper_loader() -> None:
    catalog = GeyserMcCatalog()
    resp = await catalog.search(query="floodgate", loader="fabric", game_versions=[])
    assert resp.hits == []


async def test_search_empty_beyond_first_page() -> None:
    catalog = GeyserMcCatalog()
    resp = await catalog.search(
        query="floodgate", loader="paper", game_versions=[], offset=20
    )
    assert resp.hits == []


# -- get_project --


async def test_get_project_floodgate_carries_geyser_source() -> None:
    catalog = GeyserMcCatalog()
    project = await catalog.get_project("geysermc-floodgate")
    assert project.project_id == "geysermc-floodgate"
    assert project.slug == "geysermc-floodgate"
    assert project.source == "geyser"
    assert project.loaders == ["paper"]


async def test_get_project_rejects_unhandled_project() -> None:
    catalog = GeyserMcCatalog()
    with pytest.raises(CatalogProjectNotFoundError):
        await catalog.get_project("sodium")


async def test_get_project_rejects_bare_floodgate_slug() -> None:
    """The bare ``floodgate`` slug belongs to Modrinth (issue #1961)."""
    catalog = GeyserMcCatalog()
    with pytest.raises(CatalogProjectNotFoundError):
        await catalog.get_project("floodgate")


# -- list_versions --


async def test_list_versions_parses_latest_spigot_build() -> None:
    catalog = _catalog_with_build(_BUILD)
    versions = await catalog.list_versions("geysermc-floodgate", loader="paper")
    assert len(versions) == 1
    version = versions[0]
    assert version.version_id == "2.2.5-138"
    assert version.version_number == "2.2.5"
    assert version.loaders == ["paper"]
    assert len(version.files) == 1
    file = version.files[0]
    assert file.primary is True
    assert file.filename == "floodgate-spigot.jar"
    assert file.sha512 == ""
    assert file.sha256 == _SPIGOT_SHA256
    assert file.url == (
        "https://download.geysermc.org/v2/projects/floodgate"
        "/versions/2.2.5/builds/138/downloads/spigot"
    )


async def test_list_versions_empty_for_non_paper_loader() -> None:
    catalog = _catalog_with_build(_BUILD)
    assert await catalog.list_versions("geysermc-floodgate", loader="fabric") == []


async def test_list_versions_rejects_unhandled_project() -> None:
    catalog = _catalog_with_build(_BUILD)
    with pytest.raises(CatalogProjectNotFoundError):
        await catalog.list_versions("sodium", loader="paper")


async def test_list_versions_raises_when_no_spigot_download() -> None:
    build = dict(_BUILD)
    build["downloads"] = {"velocity": {"name": "x", "sha256": "cc"}}
    catalog = _catalog_with_build(build)
    with pytest.raises(CatalogProjectNotFoundError):
        await catalog.list_versions("geysermc-floodgate", loader="paper")


# -- download_file SSRF guards (mirrors ModrinthCatalog hardening) --


async def test_download_rejects_non_https() -> None:
    catalog = GeyserMcCatalog()
    with pytest.raises(CatalogUnavailableError, match="HTTPS"):
        await catalog.download_file("http://download.geysermc.org/x")


async def test_download_rejects_disallowed_host() -> None:
    catalog = GeyserMcCatalog()
    with pytest.raises(CatalogUnavailableError, match="host not allowed"):
        await catalog.download_file("https://evil.example.com/x")


def test_assert_no_private_ips_blocks_loopback() -> None:
    with pytest.raises(CatalogUnavailableError, match="private/reserved"):
        _assert_no_private_ips(
            "download.geysermc.org", _resolver=lambda _h: ["127.0.0.1"]
        )


def test_geysermc_host_is_the_only_allowed_download_host() -> None:
    assert _ALLOWED_DOWNLOAD_HOSTS == frozenset({"download.geysermc.org"})


# -- metadata redirect follow (issue #1905 live-API regression) --


async def test_list_versions_follows_metadata_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The live ``.../builds/latest`` endpoint 302-redirects to the concrete build
    # where the JSON lives; _get_json must follow it (not just error out).
    monkeypatch.setattr(geysermc_catalog, "_resolve_host", lambda _h: ["104.18.0.1"])

    def _handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == _LATEST_URL:
            return httpx2.Response(302, headers={"location": _CONCRETE_PATH})
        if url == _CONCRETE_URL:
            return httpx2.Response(200, json=_BUILD)
        return httpx2.Response(404)

    catalog = GeyserMcCatalog()
    with _mock_transport(_handler):
        versions = await catalog.list_versions("geysermc-floodgate", loader="paper")

    assert [v.version_id for v in versions] == ["2.2.5-138"]
    assert versions[0].files[0].sha256 == _SPIGOT_SHA256


async def test_metadata_redirect_to_disallowed_host_rejected() -> None:
    def _handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"location": "https://evil.example.com/x"})

    catalog = GeyserMcCatalog()
    with _mock_transport(_handler):
        with pytest.raises(CatalogUnavailableError, match="disallowed host"):
            await catalog.list_versions("geysermc-floodgate", loader="paper")


async def test_metadata_redirect_to_private_ip_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An allowlisted host that resolves to a private IP (DNS rebinding) is still
    # rejected on the redirect hop, not only the initial request.
    monkeypatch.setattr(geysermc_catalog, "_resolve_host", lambda _h: ["127.0.0.1"])

    def _handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"location": _CONCRETE_PATH})

    catalog = GeyserMcCatalog()
    with _mock_transport(_handler):
        with pytest.raises(CatalogUnavailableError, match="private/reserved"):
            await catalog.list_versions("geysermc-floodgate", loader="paper")
