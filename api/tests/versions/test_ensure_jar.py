"""Ensure-on-start: download, verify hash, store content-addressed (FR-VER-3)."""

from __future__ import annotations

import hashlib

import pytest

from mc_server_dashboard_api.versions.adapters.composite import CompositeCatalog
from mc_server_dashboard_api.versions.adapters.vanilla import (
    _MANIFEST_URL,
    VanillaCatalog,
)
from mc_server_dashboard_api.versions.application.ensure_jar import EnsureJar
from mc_server_dashboard_api.versions.domain.errors import JarHashMismatchError
from mc_server_dashboard_api.versions.domain.value_objects import ServerType
from tests.versions.fakes import FakeJarFetcher, FakeJarPool, FakeJsonFetcher

_JAR = b"PK\x03\x04 fake jar bytes"
_JAR_URL = "https://example.test/server.jar"
_VERSION_URL = "https://example.test/1.21.1.json"


def _manifest() -> dict[str, object]:
    return {
        "versions": [{"id": "1.21.1", "type": "release", "url": _VERSION_URL}],
    }


def _detail(sha1: str) -> dict[str, object]:
    return {"downloads": {"server": {"sha1": sha1, "url": _JAR_URL}}}


def _ensure(sha1: str) -> tuple[EnsureJar, FakeJarPool, FakeJarFetcher]:
    json_fetcher = FakeJsonFetcher(
        {_MANIFEST_URL: _manifest(), _VERSION_URL: _detail(sha1)}
    )
    catalog = CompositeCatalog(
        by_type={ServerType.VANILLA: VanillaCatalog(fetcher=json_fetcher)}
    )
    jar_fetcher = FakeJarFetcher({_JAR_URL: _JAR})
    pool = FakeJarPool()
    return EnsureJar(catalog=catalog, fetcher=jar_fetcher, pool=pool), pool, jar_fetcher


@pytest.mark.asyncio
async def test_downloads_verifies_and_stores() -> None:
    good_sha1 = hashlib.sha1(_JAR).hexdigest()
    ensure, pool, _ = _ensure(good_sha1)
    key = await ensure(server_type=ServerType.VANILLA, version="1.21.1")
    assert key == hashlib.sha256(_JAR).hexdigest()
    assert pool.stored[key] == _JAR


@pytest.mark.asyncio
async def test_hash_mismatch_rejects_and_stores_nothing() -> None:
    ensure, pool, _ = _ensure("c" * 40)
    with pytest.raises(JarHashMismatchError):
        await ensure(server_type=ServerType.VANILLA, version="1.21.1")
    assert pool.stored == {}


@pytest.mark.asyncio
async def test_known_key_present_skips_download() -> None:
    good_sha1 = hashlib.sha1(_JAR).hexdigest()
    ensure, pool, jar_fetcher = _ensure(good_sha1)
    key = hashlib.sha256(_JAR).hexdigest()
    pool.stored[key] = _JAR  # already pooled
    result = await ensure(
        server_type=ServerType.VANILLA, version="1.21.1", known_key=key
    )
    assert result == key
    assert jar_fetcher.calls == []  # no re-download
