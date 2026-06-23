"""Unit tests for the client modpack zip helper (issue #1308).

Covers the two edge branches in :mod:`client_modpack_zip` that the higher-level
download tests do not pin: colliding ``filename``s are de-duplicated with a
counter suffix, and a plugin with no cached content address is skipped.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import zipfile

from mc_server_dashboard_api.servers.application.client_modpack_zip import (
    _unique_name,
    stream_client_modpack,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from tests.servers.fakes import FakePluginCacheStore

_NOW = dt.datetime(2026, 6, 15, 12, 0, 0, tzinfo=dt.timezone.utc)


def _plugin(*, filename: str, sha256: str | None) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=ServerId.new(),
        rel_path=f"mods/{filename}",
        filename=filename,
        display_name=filename,
        description=None,
        loader_type=LoaderType.MOD,
        source=PluginSource.LOCAL,
        source_project_id=None,
        source_version_id=None,
        version_number=None,
        checksum_sha512="abc",
        sha256=sha256,
        size_bytes=10,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_unique_name_dedups_with_counter_suffix() -> None:
    used: set[str] = set()
    assert _unique_name("sodium.jar", used) == "sodium.jar"
    assert _unique_name("sodium.jar", used) == "sodium (1).jar"
    assert _unique_name("sodium.jar", used) == "sodium (2).jar"
    # A distinct name is untouched.
    assert _unique_name("lithium.jar", used) == "lithium.jar"


async def test_stream_dedups_colliding_filenames() -> None:
    cache = FakePluginCacheStore()
    a = b"jar-a"
    b = b"jar-b"
    a_sha = hashlib.sha256(a).hexdigest()
    b_sha = hashlib.sha256(b).hexdigest()
    cache.blobs[a_sha] = a
    cache.blobs[b_sha] = b

    plugins = [
        _plugin(filename="mod.jar", sha256=a_sha),
        _plugin(filename="mod.jar", sha256=b_sha),  # same filename, distinct bytes
    ]

    archive = b"".join([chunk async for chunk in stream_client_modpack(cache, plugins)])
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        assert set(zf.namelist()) == {"mods/mod.jar", "mods/mod (1).jar"}
        assert zf.read("mods/mod.jar") == a
        assert zf.read("mods/mod (1).jar") == b


async def test_stream_skips_plugin_without_sha256() -> None:
    cache = FakePluginCacheStore()
    content = b"jar-bytes"
    sha = hashlib.sha256(content).hexdigest()
    cache.blobs[sha] = content

    plugins = [
        _plugin(filename="kept.jar", sha256=sha),
        _plugin(filename="skipped.jar", sha256=None),  # no cached content -> skipped
    ]

    archive = b"".join([chunk async for chunk in stream_client_modpack(cache, plugins)])
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        assert set(zf.namelist()) == {"mods/kept.jar"}


async def test_stream_sanitizes_backslash_traversal() -> None:
    """Defense-in-depth: a filename with backslash path traversal is reduced (#1400)."""
    cache = FakePluginCacheStore()
    content = b"jar-bytes"
    sha = hashlib.sha256(content).hexdigest()
    cache.blobs[sha] = content

    plugins = [_plugin(filename="a\\..\\..\\evil.jar", sha256=sha)]
    archive = b"".join([chunk async for chunk in stream_client_modpack(cache, plugins)])
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = zf.namelist()
        assert names == ["mods/evil.jar"]


async def test_stream_sanitizes_forward_slash() -> None:
    """Defense-in-depth: a filename with forward slash is reduced (#1400)."""
    cache = FakePluginCacheStore()
    content = b"jar-bytes"
    sha = hashlib.sha256(content).hexdigest()
    cache.blobs[sha] = content

    plugins = [_plugin(filename="subdir/evil.jar", sha256=sha)]
    archive = b"".join([chunk async for chunk in stream_client_modpack(cache, plugins)])
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = zf.namelist()
        assert names == ["mods/evil.jar"]


async def test_stream_sanitizes_mixed_separators() -> None:
    """Defense-in-depth: a filename with mixed separators is reduced (#1400)."""
    cache = FakePluginCacheStore()
    content = b"jar-bytes"
    sha = hashlib.sha256(content).hexdigest()
    cache.blobs[sha] = content

    plugins = [_plugin(filename="a/b\\c.jar", sha256=sha)]
    archive = b"".join([chunk async for chunk in stream_client_modpack(cache, plugins)])
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = zf.namelist()
        assert names == ["mods/c.jar"]


async def test_stream_dedups_after_sanitization() -> None:
    """Filenames that share a basename after sanitization are deduped (#1400)."""
    cache = FakePluginCacheStore()
    a = b"jar-a"
    b = b"jar-b"
    a_sha = hashlib.sha256(a).hexdigest()
    b_sha = hashlib.sha256(b).hexdigest()
    cache.blobs[a_sha] = a
    cache.blobs[b_sha] = b

    plugins = [
        _plugin(filename="a/evil.jar", sha256=a_sha),
        _plugin(filename="b/evil.jar", sha256=b_sha),
    ]
    archive = b"".join([chunk async for chunk in stream_client_modpack(cache, plugins)])
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        assert set(zf.namelist()) == {"mods/evil.jar", "mods/evil (1).jar"}
        assert zf.read("mods/evil.jar") == a
        assert zf.read("mods/evil (1).jar") == b
