"""Validate and normalize a Minecraft resource pack zip (issue #1192).

Minecraft requires ``pack.mcmeta`` at the zip root. Common user-created zips
wrap everything under a subdirectory. This module detects the structure and
normalizes it so the pack works when served to clients.
"""

from __future__ import annotations

import io
import json
import os
import zipfile

from mc_server_dashboard_api.servers.domain.errors import InvalidResourcePackError

# Safety limits.
_MAX_DECOMPRESSED_BYTES = 512 * 1024 * 1024  # 512 MiB
_MAX_ENTRY_COUNT = 10_000
_MAX_ZIP_IN_ZIP_DEPTH = 3


def validate_and_normalize(content: bytes, *, _depth: int = 0) -> bytes:
    """Validate and normalize a resource pack zip.

    Returns the (possibly repacked) bytes with ``pack.mcmeta`` at the root.
    Raises :class:`InvalidResourcePackError` on validation failure.
    """
    if _depth > _MAX_ZIP_IN_ZIP_DEPTH:
        raise InvalidResourcePackError("zip-in-zip depth limit exceeded")

    # Step 1: Ensure it's a valid zip.
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise InvalidResourcePackError("not a valid zip")

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        infos = zf.infolist()

        # Safety: entry count.
        if len(infos) > _MAX_ENTRY_COUNT:
            raise InvalidResourcePackError("too many entries")

        # Safety: path traversal and decompressed size.
        # Count actual decompressed bytes — ``info.file_size`` is attacker-
        # controllable (zip header field), so it must not be trusted (#1221).
        total_size = 0
        for info in infos:
            name = info.filename
            if name.startswith("/") or ".." in name.split("/"):
                raise InvalidResourcePackError(f"path traversal: {name}")
            if not info.is_dir():
                total_size += len(zf.read(name))
                if total_size > _MAX_DECOMPRESSED_BYTES:
                    raise InvalidResourcePackError("decompressed size exceeds limit")

        # Step 2: Check for zip-in-zip (single entry that is a .zip file).
        non_dir = [i for i in infos if not i.is_dir()]
        if len(non_dir) == 1 and non_dir[0].filename.lower().endswith(".zip"):
            inner = zf.read(non_dir[0].filename)
            return validate_and_normalize(inner, _depth=_depth + 1)

        # Step 3: Find pack.mcmeta entries.
        mcmeta_paths = [
            i.filename
            for i in infos
            if os.path.basename(i.filename) == "pack.mcmeta" and not i.is_dir()
        ]
        if not mcmeta_paths:
            raise InvalidResourcePackError("pack.mcmeta not found")

        # Step 4: Pick the shallowest pack.mcmeta.
        mcmeta_paths.sort(key=lambda p: p.count("/"))
        shallowest_depth = mcmeta_paths[0].count("/")
        tied = [p for p in mcmeta_paths if p.count("/") == shallowest_depth]
        if len(tied) > 1:
            raise InvalidResourcePackError(
                "ambiguous: multiple pack.mcmeta at the same depth"
            )
        target = tied[0]

        # Step 5: Validate pack.mcmeta content.
        _validate_pack_mcmeta(zf.read(target))

        # Step 6: Determine prefix to strip.
        prefix = os.path.dirname(target)
        if not prefix:
            # Already at root — return as-is.
            return content

        # Step 7: Repack with prefix stripped.
        return _repack_stripping_prefix(zf, prefix)


def _validate_pack_mcmeta(raw: bytes) -> None:
    """Validate that raw bytes are a valid pack.mcmeta."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise InvalidResourcePackError("invalid pack.mcmeta: not valid JSON")

    if not isinstance(data, dict):
        raise InvalidResourcePackError("invalid pack.mcmeta: not a JSON object")

    pack = data.get("pack")
    if not isinstance(pack, dict):
        raise InvalidResourcePackError("invalid pack.mcmeta: missing 'pack' key")

    fmt = pack.get("pack_format")
    if not isinstance(fmt, int) or isinstance(fmt, bool):
        raise InvalidResourcePackError(
            "invalid pack.mcmeta: pack_format must be an integer"
        )


def _repack_stripping_prefix(zf: zipfile.ZipFile, prefix: str) -> bytes:
    """Create a new zip with ``prefix + '/'`` stripped from all entry paths.

    Entries whose path does not start with the prefix are dropped (they belong
    to a sibling directory, not to the resource pack).
    """
    prefix_slash = prefix + "/"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for info in zf.infolist():
            if not info.filename.startswith(prefix_slash):
                continue
            new_name = info.filename[len(prefix_slash) :]
            if not new_name:
                continue  # skip the directory entry for the prefix itself
            out.writestr(new_name, zf.read(info.filename))
    return buf.getvalue()
