"""Tests for resource pack zip validation and normalization (issue #1192).

Constructs zips programmatically with ``zipfile.ZipFile`` + ``io.BytesIO``.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from mc_server_dashboard_api.servers.application.resource_pack_zip import (
    validate_and_normalize,
)
from mc_server_dashboard_api.servers.domain.errors import InvalidResourcePackError

_VALID_MCMETA = json.dumps({"pack": {"pack_format": 15, "description": "Test"}})


def _make_zip(entries: dict[str, str | bytes]) -> bytes:
    """Build a zip in memory from {path: content} pairs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestValidPackAtRoot:
    def test_accepted_as_is(self) -> None:
        content = _make_zip(
            {
                "pack.mcmeta": _VALID_MCMETA,
                "assets/minecraft/textures/block/stone.png": b"PNG",
            }
        )
        result = validate_and_normalize(content)
        # Verify pack.mcmeta is at root in the result
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            assert "pack.mcmeta" in zf.namelist()
            mcmeta = json.loads(zf.read("pack.mcmeta"))
            assert mcmeta["pack"]["pack_format"] == 15


class TestFolderWrappedSingleLevel:
    def test_prefix_stripped(self) -> None:
        content = _make_zip(
            {
                "SomePack/pack.mcmeta": _VALID_MCMETA,
                "SomePack/assets/minecraft/textures/block/stone.png": b"PNG",
            }
        )
        result = validate_and_normalize(content)
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            names = zf.namelist()
            assert "pack.mcmeta" in names
            assert "assets/minecraft/textures/block/stone.png" in names
            # Original prefixed entries must not be present
            assert "SomePack/pack.mcmeta" not in names


class TestDeepNested:
    def test_multi_level_prefix_stripped(self) -> None:
        content = _make_zip(
            {
                "a/b/c/pack.mcmeta": _VALID_MCMETA,
                "a/b/c/assets/test.png": b"PNG",
            }
        )
        result = validate_and_normalize(content)
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            names = zf.namelist()
            assert "pack.mcmeta" in names
            assert "assets/test.png" in names


class TestZipInZip:
    def test_inner_zip_extracted_and_used(self) -> None:
        inner = _make_zip(
            {
                "pack.mcmeta": _VALID_MCMETA,
                "assets/test.png": b"PNG",
            }
        )
        outer = _make_zip({"inner.zip": inner})
        result = validate_and_normalize(outer)
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            assert "pack.mcmeta" in zf.namelist()

    def test_zip_in_zip_nested(self) -> None:
        """Two levels of wrapping (inner-inner and inner)."""
        innermost = _make_zip(
            {
                "pack.mcmeta": _VALID_MCMETA,
                "assets/test.png": b"PNG",
            }
        )
        middle = _make_zip({"innermost.zip": innermost})
        outer = _make_zip({"middle.zip": middle})
        result = validate_and_normalize(outer)
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            assert "pack.mcmeta" in zf.namelist()

    def test_zip_in_zip_depth_exceeded(self) -> None:
        """Exceeding the depth limit of 3 raises an error."""
        z = _make_zip({"pack.mcmeta": _VALID_MCMETA})
        for i in range(4):
            z = _make_zip({f"level{i}.zip": z})
        with pytest.raises(InvalidResourcePackError, match="depth"):
            validate_and_normalize(z)


class TestNotAZip:
    def test_random_bytes_rejected(self) -> None:
        with pytest.raises(InvalidResourcePackError, match="not a valid zip"):
            validate_and_normalize(b"this is not a zip file")

    def test_empty_bytes_rejected(self) -> None:
        with pytest.raises(InvalidResourcePackError, match="not a valid zip"):
            validate_and_normalize(b"")


class TestNoPackMcmeta:
    def test_no_mcmeta_rejected(self) -> None:
        content = _make_zip({"assets/test.png": b"PNG"})
        with pytest.raises(InvalidResourcePackError, match="pack.mcmeta not found"):
            validate_and_normalize(content)


class TestInvalidPackMcmeta:
    def test_not_json(self) -> None:
        content = _make_zip({"pack.mcmeta": "not json {"})
        with pytest.raises(InvalidResourcePackError, match="invalid pack.mcmeta"):
            validate_and_normalize(content)

    def test_missing_pack_format(self) -> None:
        content = _make_zip(
            {
                "pack.mcmeta": json.dumps({"pack": {"description": "no format"}}),
            }
        )
        with pytest.raises(InvalidResourcePackError, match="invalid pack.mcmeta"):
            validate_and_normalize(content)

    def test_pack_format_not_int(self) -> None:
        content = _make_zip(
            {
                "pack.mcmeta": json.dumps({"pack": {"pack_format": "15"}}),
            }
        )
        with pytest.raises(InvalidResourcePackError, match="invalid pack.mcmeta"):
            validate_and_normalize(content)

    def test_missing_pack_key(self) -> None:
        content = _make_zip(
            {
                "pack.mcmeta": json.dumps({"other": "stuff"}),
            }
        )
        with pytest.raises(InvalidResourcePackError, match="invalid pack.mcmeta"):
            validate_and_normalize(content)


class TestMultiplePackMcmeta:
    def test_shallowest_picked(self) -> None:
        content = _make_zip(
            {
                "top/pack.mcmeta": _VALID_MCMETA,
                "top/sub/pack.mcmeta": json.dumps(
                    {"pack": {"pack_format": 1, "description": "deeper"}}
                ),
                "top/assets/test.png": b"PNG",
            }
        )
        result = validate_and_normalize(content)
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            mcmeta = json.loads(zf.read("pack.mcmeta"))
            # The shallowest (top/pack.mcmeta with format 15) should be used
            assert mcmeta["pack"]["pack_format"] == 15

    def test_ambiguous_tied_depth_rejected(self) -> None:
        content = _make_zip(
            {
                "dir_a/pack.mcmeta": _VALID_MCMETA,
                "dir_b/pack.mcmeta": _VALID_MCMETA,
            }
        )
        with pytest.raises(InvalidResourcePackError, match="ambiguous"):
            validate_and_normalize(content)


class TestZipBomb:
    def test_exceeds_decompressed_size(self) -> None:
        """Lowering the limit makes a normal zip exceed it."""
        import mc_server_dashboard_api.servers.application.resource_pack_zip as mod

        content = _make_zip(
            {
                "pack.mcmeta": _VALID_MCMETA,
                "assets/big.bin": b"x" * 1000,
            }
        )
        original = mod._MAX_DECOMPRESSED_BYTES
        mod._MAX_DECOMPRESSED_BYTES = 100  # artificially low
        try:
            with pytest.raises(InvalidResourcePackError, match="decompressed size"):
                validate_and_normalize(content)
        finally:
            mod._MAX_DECOMPRESSED_BYTES = original

    def test_repack_counts_actual_bytes(self) -> None:
        """Repacking a prefix-wrapped zip must enforce the decompressed limit.

        Before the fix, ``_repack_stripping_prefix`` called ``zf.read()``
        without any cumulative byte counting — only the header-based pre-scan
        guarded size, and it could be bypassed.  This test uses a
        prefix-wrapped zip whose total decompressed content exceeds the
        (lowered) limit and asserts the guard fires during repacking.
        """
        import mc_server_dashboard_api.servers.application.resource_pack_zip as mod

        content = _make_zip(
            {
                "SomePack/pack.mcmeta": _VALID_MCMETA,
                "SomePack/assets/big.bin": b"x" * 1000,
            }
        )
        original = mod._MAX_DECOMPRESSED_BYTES
        mod._MAX_DECOMPRESSED_BYTES = 100  # artificially low
        try:
            with pytest.raises(InvalidResourcePackError, match="decompressed size"):
                validate_and_normalize(content)
        finally:
            mod._MAX_DECOMPRESSED_BYTES = original

    def test_single_entry_bomb_caught_during_chunked_read(self) -> None:
        """A single entry larger than the limit is caught mid-read (#1252).

        With chunked reads, the size cap fires after a few 64 KiB chunks
        rather than after the full entry is decompressed into memory.
        """
        import mc_server_dashboard_api.servers.application.resource_pack_zip as mod

        # A single highly-compressible entry (200 KiB of zeros).
        big_data = b"\x00" * (200 * 1024)
        content = _make_zip(
            {
                "pack.mcmeta": _VALID_MCMETA,
                "assets/bomb.bin": big_data,
            }
        )
        original_max = mod._MAX_DECOMPRESSED_BYTES
        # Set limit below the entry size but above the chunk size (64 KiB)
        # so the check fires after a few chunks, not after full read.
        mod._MAX_DECOMPRESSED_BYTES = 100 * 1024  # 100 KiB
        try:
            with pytest.raises(InvalidResourcePackError, match="decompressed size"):
                validate_and_normalize(content)
        finally:
            mod._MAX_DECOMPRESSED_BYTES = original_max

    def test_exceeds_entry_count(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("pack.mcmeta", _VALID_MCMETA)
            for i in range(10_001):
                zf.writestr(f"assets/file_{i}.txt", "x")
        with pytest.raises(InvalidResourcePackError, match="too many entries"):
            validate_and_normalize(buf.getvalue())


class TestPathTraversal:
    def test_dotdot_rejected(self) -> None:
        content = _make_zip(
            {
                "pack.mcmeta": _VALID_MCMETA,
                "../etc/passwd": b"evil",
            }
        )
        with pytest.raises(InvalidResourcePackError, match="path traversal"):
            validate_and_normalize(content)

    def test_absolute_path_rejected(self) -> None:
        content = _make_zip(
            {
                "pack.mcmeta": _VALID_MCMETA,
                "/etc/passwd": b"evil",
            }
        )
        with pytest.raises(InvalidResourcePackError, match="path traversal"):
            validate_and_normalize(content)
