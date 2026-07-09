"""Ensure-on-start: download, verify hash, store content-addressed (FR-VER-3)."""

from __future__ import annotations

import hashlib
import logging

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
from mc_server_dashboard_api.versions.domain.errors import (
    CatalogUnavailableError,
    JarDownloadError,
    JarHashMismatchError,
)
from mc_server_dashboard_api.versions.domain.value_objects import JarSource, ServerType
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
    result = await ensure(server_type=ServerType.VANILLA, version="1.21.1")
    assert result.key == hashlib.sha256(_JAR).hexdigest()
    assert result.source_fingerprint == f"sha1:{good_sha1}"
    assert pool.stored[result.key] == _JAR


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
    result = await ensure(server_type=ServerType.FABRIC, version="1.21.1")
    assert result.key == hashlib.sha256(_FABRIC_JAR).hexdigest()
    assert result.source_fingerprint == f"url:{_FABRIC_JAR_URL}"
    assert pool.stored[result.key] == _FABRIC_JAR


_FORGE_INSTALLER = b"PK\x03\x04 forge installer jar"


def _forge_ensure() -> tuple[EnsureJar, FakeJarPool]:
    from mc_server_dashboard_api.versions.adapters.forge import (
        _METADATA_URL,
        _PROMOTIONS_URL,
        ForgeCatalog,
        _installer_sha1_url,
        _installer_url,
    )
    from tests.versions.fakes import FakeDocumentFetcher

    metadata = (
        "<metadata><versioning><versions>"
        "<version>1.21.8-58.1.0</version>"
        "</versions></versioning></metadata>"
    )
    promotions = {"promos": {"1.21.8-recommended": "58.1.0"}}
    installer_url = _installer_url("1.21.8-58.1.0")
    good_sha1 = hashlib.sha1(_FORGE_INSTALLER).hexdigest()
    doc_fetcher = FakeDocumentFetcher(
        texts={
            _METADATA_URL: metadata,
            _installer_sha1_url("1.21.8-58.1.0"): good_sha1,
        },
        payloads={_PROMOTIONS_URL: promotions},
    )
    catalog = CompositeCatalog(
        by_type={ServerType.FORGE: ForgeCatalog(fetcher=doc_fetcher)}
    )
    jar_fetcher = FakeJarFetcher({installer_url: _FORGE_INSTALLER})
    pool = FakeJarPool()
    return EnsureJar(catalog=catalog, fetcher=jar_fetcher, pool=pool), pool


@pytest.mark.asyncio
async def test_forge_pools_installer_verified_by_upstream_sha1() -> None:
    # Forge pools the INSTALLER jar; the upstream .sha1 is verified through the
    # existing SHA-1 seam (like vanilla) before the bytes are stored (issue #307).
    ensure, pool = _forge_ensure()
    result = await ensure(server_type=ServerType.FORGE, version="1.21.8")
    assert result.key == hashlib.sha256(_FORGE_INSTALLER).hexdigest()
    assert pool.stored[result.key] == _FORGE_INSTALLER


@pytest.mark.asyncio
async def test_known_key_present_skips_download_when_fingerprint_matches() -> None:
    good_sha1 = hashlib.sha1(_JAR).hexdigest()
    ensure, pool, jar_fetcher = _ensure(good_sha1)
    key = hashlib.sha256(_JAR).hexdigest()
    pool.stored[key] = _JAR  # already pooled
    fingerprint = f"sha1:{good_sha1}"
    result = await ensure(
        server_type=ServerType.VANILLA,
        version="1.21.1",
        known_key=key,
        known_source=fingerprint,
    )
    assert result.key == key
    assert result.source_fingerprint == fingerprint
    assert jar_fetcher.calls == []  # no re-download


@pytest.mark.asyncio
async def test_migration_vanilla_re_downloads_once_without_known_source() -> None:
    """Pre-#1676 vanilla server: pooled key + no known_source + SHA-1 source.

    The SHA-256 shortcut does not apply (source is SHA-1), and known_source is
    None so the fingerprint comparison fails.  The JAR is re-downloaded once,
    returning the same key and the new fingerprint for future starts.
    """
    good_sha1 = hashlib.sha1(_JAR).hexdigest()
    ensure, pool, jar_fetcher = _ensure(good_sha1)
    key = hashlib.sha256(_JAR).hexdigest()
    pool.stored[key] = _JAR  # already pooled

    result = await ensure(
        server_type=ServerType.VANILLA,
        version="1.21.1",
        known_key=key,
        # No known_source: pre-#1676 server
    )

    assert result.key == key
    assert result.source_fingerprint == f"sha1:{good_sha1}"
    # One-time re-download (fingerprint mismatch: None != "sha1:...").
    assert jar_fetcher.calls == [_JAR_URL]


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
    result = await ensure(server_type=ServerType.VANILLA, version="1.21.1")
    key = result.key
    assert key in pool.stored

    class _NoReferences(LiveJarReferences):
        async def live(self) -> set[str]:
            return set()

    class _PastClock(Clock):
        # Far enough ahead that the freshly-stored JAR is past the safety window.
        def now(self) -> dt.datetime:
            return dt.datetime.now(dt.UTC) + GC_SAFETY_WINDOW + dt.timedelta(hours=1)

    gc = RunJarPoolGc(pool=pool, references=_NoReferences(), clock=_PastClock())
    gc_result = await gc()
    assert gc_result.deleted == 1
    assert key not in pool.stored

    # The recorded content key is no longer pooled, so ensure re-downloads it.
    jar_fetcher.calls.clear()
    re_result = await ensure(
        server_type=ServerType.VANILLA, version="1.21.1", known_key=key
    )
    assert re_result.key == key
    assert pool.stored[key] == _JAR
    assert jar_fetcher.calls == [_JAR_URL]


# --- issue #1676: auto-update detection ------------------------------------

_NEW_JAR = b"PK\x03\x04 updated jar bytes"
_NEW_JAR_URL = "https://example.test/server-v2.jar"


def _new_detail(sha1: str) -> dict[str, object]:
    return {"downloads": {"server": {"sha1": sha1, "url": _NEW_JAR_URL}}}


@pytest.mark.asyncio
async def test_fingerprint_mismatch_downloads_new_jar() -> None:
    """When the catalog resolves a different build, the new JAR is downloaded."""
    old_sha1 = hashlib.sha1(_JAR).hexdigest()
    new_sha1 = hashlib.sha1(_NEW_JAR).hexdigest()
    old_key = hashlib.sha256(_JAR).hexdigest()

    # Build the catalog with the NEW jar details, but seed the pool with the OLD.
    json_fetcher = FakeJsonFetcher(
        {_MANIFEST_URL: _manifest(), _VERSION_URL: _new_detail(new_sha1)}
    )
    catalog = CompositeCatalog(
        by_type={ServerType.VANILLA: VanillaCatalog(fetcher=json_fetcher)}
    )
    jar_fetcher = FakeJarFetcher({_NEW_JAR_URL: _NEW_JAR})
    pool = FakeJarPool()
    pool.stored[old_key] = _JAR  # old JAR still pooled
    ensure = EnsureJar(catalog=catalog, fetcher=jar_fetcher, pool=pool)

    result = await ensure(
        server_type=ServerType.VANILLA,
        version="1.21.1",
        known_key=old_key,
        known_source=f"sha1:{old_sha1}",
    )

    new_key = hashlib.sha256(_NEW_JAR).hexdigest()
    assert result.key == new_key
    assert result.source_fingerprint == f"sha1:{new_sha1}"
    assert jar_fetcher.calls == [_NEW_JAR_URL]  # downloaded the new build


@pytest.mark.asyncio
async def test_paper_sha256_shortcut_skips_download_without_known_source() -> None:
    """Paper uses SHA-256: when pooled key == published hash, skip download even
    without a recorded fingerprint (back-compat for pre-#1676 servers)."""
    from mc_server_dashboard_api.versions.adapters.paper import (
        _BASE,
        PaperCatalog,
    )

    jar_sha256 = hashlib.sha256(_JAR).hexdigest()
    download_url = "https://fill-data.papermc.io/paper-1.21.1-42.jar"
    json_fetcher = FakeJsonFetcher(
        {
            f"{_BASE}/versions/1.21.1/builds/latest": {
                "downloads": {
                    "server:default": {
                        "name": "paper-1.21.1-42.jar",
                        "url": download_url,
                        "checksums": {"sha256": jar_sha256},
                    }
                },
            },
        }
    )
    catalog = CompositeCatalog(
        by_type={ServerType.PAPER: PaperCatalog(fetcher=json_fetcher)}
    )
    pool = FakeJarPool()
    pool.stored[jar_sha256] = _JAR  # already pooled under its own sha256
    jar_fetcher = FakeJarFetcher({})
    ensure = EnsureJar(catalog=catalog, fetcher=jar_fetcher, pool=pool)

    result = await ensure(
        server_type=ServerType.PAPER,
        version="1.21.1",
        known_key=jar_sha256,
        # No known_source: pre-#1676 server
    )

    assert result.key == jar_sha256
    assert result.source_fingerprint == f"sha256:{jar_sha256}"
    assert jar_fetcher.calls == []  # no download


class _FailingCatalog(CompositeCatalog):
    """A catalog that raises CatalogUnavailableError on resolve."""

    async def resolve(self, server_type: ServerType, version: str) -> JarSource:
        raise CatalogUnavailableError("catalog down")


@pytest.mark.asyncio
async def test_resolve_failure_with_pooled_key_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When catalog.resolve fails but the existing JAR is pooled, fall back."""
    good_sha1 = hashlib.sha1(_JAR).hexdigest()
    catalog = _FailingCatalog(by_type={})
    pool = FakeJarPool()
    key = hashlib.sha256(_JAR).hexdigest()
    pool.stored[key] = _JAR
    jar_fetcher = FakeJarFetcher({})
    ensure = EnsureJar(catalog=catalog, fetcher=jar_fetcher, pool=pool)

    old_fingerprint = f"sha1:{good_sha1}"
    with caplog.at_level(logging.WARNING):
        result = await ensure(
            server_type=ServerType.VANILLA,
            version="1.21.1",
            known_key=key,
            known_source=old_fingerprint,
        )

    assert result.key == key
    assert result.source_fingerprint == old_fingerprint
    assert "falling back to pooled" in caplog.text


@pytest.mark.asyncio
async def test_resolve_failure_without_pooled_key_raises() -> None:
    """When catalog.resolve fails and no existing JAR is pooled, raise."""
    catalog = _FailingCatalog(by_type={})
    pool = FakeJarPool()
    jar_fetcher = FakeJarFetcher({})
    ensure = EnsureJar(catalog=catalog, fetcher=jar_fetcher, pool=pool)

    with pytest.raises(CatalogUnavailableError):
        await ensure(server_type=ServerType.VANILLA, version="1.21.1")


class _FailingJarFetcher(FakeJarFetcher):
    """A JAR fetcher that raises JarDownloadError on every fetch."""

    async def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        raise JarDownloadError(f"download failed for {url}")


@pytest.mark.asyncio
async def test_download_failure_with_pooled_key_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When download of a new build fails but the old JAR is pooled, fall back."""
    old_sha1 = hashlib.sha1(_JAR).hexdigest()
    # Catalog publishes a DIFFERENT hash than what we recorded, so the
    # fingerprint mismatches and the code proceeds to download.
    new_sha1 = hashlib.sha1(_NEW_JAR).hexdigest()
    json_fetcher = FakeJsonFetcher(
        {_MANIFEST_URL: _manifest(), _VERSION_URL: _new_detail(new_sha1)}
    )
    catalog = CompositeCatalog(
        by_type={ServerType.VANILLA: VanillaCatalog(fetcher=json_fetcher)}
    )
    pool = FakeJarPool()
    key = hashlib.sha256(_JAR).hexdigest()
    pool.stored[key] = _JAR
    jar_fetcher = _FailingJarFetcher({})
    ensure = EnsureJar(catalog=catalog, fetcher=jar_fetcher, pool=pool)

    old_fingerprint = f"sha1:{old_sha1}"
    with caplog.at_level(logging.WARNING):
        result = await ensure(
            server_type=ServerType.VANILLA,
            version="1.21.1",
            known_key=key,
            known_source=old_fingerprint,
        )

    assert result.key == key
    assert result.source_fingerprint == old_fingerprint
    assert "falling back to pooled" in caplog.text


@pytest.mark.asyncio
async def test_download_failure_without_pooled_key_raises() -> None:
    """When download fails and no existing JAR is pooled, raise."""
    good_sha1 = hashlib.sha1(_JAR).hexdigest()
    json_fetcher = FakeJsonFetcher(
        {_MANIFEST_URL: _manifest(), _VERSION_URL: _detail(good_sha1)}
    )
    catalog = CompositeCatalog(
        by_type={ServerType.VANILLA: VanillaCatalog(fetcher=json_fetcher)}
    )
    pool = FakeJarPool()
    jar_fetcher = _FailingJarFetcher({})
    ensure = EnsureJar(catalog=catalog, fetcher=jar_fetcher, pool=pool)

    with pytest.raises(JarDownloadError):
        await ensure(server_type=ServerType.VANILLA, version="1.21.1")
