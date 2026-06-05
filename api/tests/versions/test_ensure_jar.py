"""Ensure-on-start: download, verify hash, store content-addressed (FR-VER-3)."""

from __future__ import annotations

import hashlib

import pytest

from mc_server_dashboard_api.versions.adapters.composite import CompositeCatalog
from mc_server_dashboard_api.versions.adapters.fabric import (
    _GAME_URL,
    _INSTALLER_URL,
    _LOADER_URL,
    FabricCatalog,
    _loader_for_game_url,
    _server_jar_url,
)
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


_FABRIC_JAR = b"PK\x03\x04 fabric launcher jar"
_FABRIC_JAR_URL = _server_jar_url("1.21.1", "0.16.5", "1.0.1")


def _fabric_ensure() -> tuple[EnsureJar, FakeJarPool]:
    json_fetcher = FakeJsonFetcher(
        {
            _GAME_URL: [{"version": "1.21.1", "stable": True}],
            _LOADER_URL: [{"version": "0.16.5", "stable": True}],
            _INSTALLER_URL: [{"version": "1.0.1", "stable": True}],
            _loader_for_game_url("1.21.1"): [{"loader": {"version": "0.16.5"}}],
        }
    )
    catalog = CompositeCatalog(
        by_type={ServerType.FABRIC: FabricCatalog(fetcher=json_fetcher)}
    )
    jar_fetcher = FakeJarFetcher({_FABRIC_JAR_URL: _FABRIC_JAR})
    pool = FakeJarPool()
    return EnsureJar(catalog=catalog, fetcher=jar_fetcher, pool=pool), pool


@pytest.mark.asyncio
async def test_fabric_downloads_and_stores_without_checksum() -> None:
    # Fabric publishes no digest for the generated launcher JAR: ensure-on-start
    # stores the bytes unverified, content-addressed by their own SHA-256.
    ensure, pool = _fabric_ensure()
    key = await ensure(server_type=ServerType.FABRIC, version="1.21.1")
    assert key == hashlib.sha256(_FABRIC_JAR).hexdigest()
    assert pool.stored[key] == _FABRIC_JAR


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


@pytest.mark.asyncio
async def test_ensure_after_gc_re_downloads_cleanly() -> None:
    # After the GC reclaims an orphaned JAR, a later start that needs the same
    # version finds it gone (known_key present but not pooled) and re-downloads,
    # leaving the pool restored (#293).
    import datetime as dt

    from mc_server_dashboard_api.versions.application.jar_gc import (
        GC_SAFETY_WINDOW,
        RunJarPoolGc,
    )
    from mc_server_dashboard_api.versions.domain.clock import Clock
    from mc_server_dashboard_api.versions.domain.jar_references import LiveJarReferences

    good_sha1 = hashlib.sha1(_JAR).hexdigest()
    ensure, pool, jar_fetcher = _ensure(good_sha1)
    key = await ensure(server_type=ServerType.VANILLA, version="1.21.1")
    assert key in pool.stored

    class _NoReferences(LiveJarReferences):
        async def live(self) -> set[str]:
            return set()

    class _PastClock(Clock):
        # Far enough ahead that the freshly-stored JAR is past the safety window.
        def now(self) -> dt.datetime:
            return dt.datetime.now(dt.UTC) + GC_SAFETY_WINDOW + dt.timedelta(hours=1)

    gc = RunJarPoolGc(pool=pool, references=_NoReferences(), clock=_PastClock())
    result = await gc()
    assert result.deleted == 1
    assert key not in pool.stored

    # The recorded content key is no longer pooled, so ensure re-downloads it.
    jar_fetcher.calls.clear()
    re_key = await ensure(
        server_type=ServerType.VANILLA, version="1.21.1", known_key=key
    )
    assert re_key == key
    assert pool.stored[key] == _JAR
    assert jar_fetcher.calls == [_JAR_URL]
