"""Parse a jar's embedded manifest into structured metadata (issue #1307).

A plugin/mod ``.jar`` is a zip that carries a loader-specific descriptor at a
known path. This module reads that descriptor and returns a uniform
:class:`ParsedManifest` -- the single dependency-metadata source for both local
uploads and Modrinth installs. It is pure parsing: no storage, no DB, no network.

Unlike the global-library variant this was ported from (#1271), the per-server
model already knows the loader family at ingest (from the server's
``ServerType``), so the caller tells the parser which family to look for and the
result needs no ``loader_type`` field. Manifest files per family:

============  ==============================  ==========================
Family        File                            Format
============  ==============================  ==========================
fabric        ``fabric.mod.json``             JSON (root)
fabric        ``quilt.mod.json``              JSON (``quilt_loader.*``)
forge         ``META-INF/mods.toml``          TOML
forge         ``META-INF/neoforge.mods.toml`` TOML
paper         ``paper-plugin.yml`` / ``plugin.yml``  YAML subset
============  ==============================  ==========================

(A ``fabric`` server runs Fabric or Quilt mods; a ``forge`` server runs Forge or
NeoForge mods, so each family tries both of its descriptors.)

Robustness contract:

* Bytes that do not open as a zip, carry too many entries, or decompress past
  the size cap raise :class:`InvalidModJarError`.
* A readable zip with no recognized manifest -- or a garbled/identity-less one --
  returns :meth:`ParsedManifest.empty` rather than raising. The install still
  succeeds (the loader is known from the server type); the jar simply carries no
  dependency metadata.
"""

from __future__ import annotations

import io
import json
import tomllib
import zipfile
from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.domain.errors import InvalidModJarError

# Safety limits (resource_pack_zip.py precedent, plugin upload hardening).
_MAX_DECOMPRESSED_BYTES = 512 * 1024 * 1024  # 512 MiB (matches the plugin upload cap)
_MAX_ENTRY_COUNT = 50_000
_CHUNK_SIZE = 64 * 1024  # 64 KiB

# Manifest paths (case-sensitive, matched against the zip entry name).
_FABRIC_MANIFEST = "fabric.mod.json"
_QUILT_MANIFEST = "quilt.mod.json"
_FORGE_MANIFEST = "META-INF/mods.toml"
_NEOFORGE_MANIFEST = "META-INF/neoforge.mods.toml"
_PAPER_MANIFEST = "paper-plugin.yml"
_BUKKIT_MANIFEST = "plugin.yml"

# Which manifest files each server type's parser reads, in priority order.
_FAMILY_MANIFESTS: dict[str, tuple[str, ...]] = {
    "fabric": (_FABRIC_MANIFEST, _QUILT_MANIFEST),
    "forge": (_NEOFORGE_MANIFEST, _FORGE_MANIFEST),
    "paper": (_PAPER_MANIFEST, _BUKKIT_MANIFEST),
}

# Fabric/Quilt ids that are loader/runtime constraints, not real mod deps.
_FABRIC_RUNTIME_IDS = frozenset({"minecraft", "java", "fabricloader", "fabric"})
# Forge/NeoForge dependency ids that are loader/runtime constraints.
_FORGE_RUNTIME_IDS = frozenset({"minecraft", "forge", "neoforge"})

# Forge/NeoForge dependency ``type`` values that mark an incompatibility.
_FORGE_CONFLICT_TYPES = frozenset({"incompatible", "discouraged"})


@dataclass(frozen=True)
class ParsedManifest:
    """Dependency metadata extracted from a jar manifest.

    ``dependencies`` use the persisted shape ``[{"mod_identifier": str,
    "version_range": str, "required": bool, "conflict": bool}]``. ``conflict``
    flags an entry as an incompatibility (Fabric/Quilt ``breaks``,
    Forge/NeoForge ``incompatible``/``discouraged``) rather than a dependency; it
    defaults ``False`` for ordinary deps. ``mod_identifier`` is ``""`` when no
    usable manifest was found.
    """

    mod_identifier: str
    provides: list[str] = field(default_factory=list)
    mc_versions: list[str] = field(default_factory=list)
    dependencies: list[dict[str, object]] = field(default_factory=list)

    @classmethod
    def empty(cls) -> ParsedManifest:
        """The result for a readable jar with no usable manifest."""

        return cls(mod_identifier="")


def parse_manifest(jar_bytes: bytes, *, server_type: str) -> ParsedManifest:
    """Parse a jar's manifest into :class:`ParsedManifest` for ``server_type``.

    ``server_type`` selects which loader descriptors to read (``fabric`` ->
    fabric/quilt, ``forge`` -> forge/neoforge, ``paper`` -> paper/bukkit). Raises
    :class:`InvalidModJarError` if the bytes are not a readable, bounded zip.
    Returns :meth:`ParsedManifest.empty` if no usable manifest is found.
    """

    manifests = _FAMILY_MANIFESTS.get(server_type)
    if manifests is None:
        return ParsedManifest.empty()

    if not zipfile.is_zipfile(io.BytesIO(jar_bytes)):
        raise InvalidModJarError("not a valid jar (zip)")

    names: dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(jar_bytes)) as zf:
            infos = zf.infolist()
            if len(infos) > _MAX_ENTRY_COUNT:
                raise InvalidModJarError("too many entries")
            wanted = set(manifests)
            for info in infos:
                if info.is_dir() or info.filename not in wanted:
                    continue
                names[info.filename] = _read_entry_chunked(zf, info.filename)
    except zipfile.BadZipFile as exc:
        raise InvalidModJarError("corrupt jar (zip)") from exc

    for manifest_name in manifests:
        raw = names.get(manifest_name)
        if raw is None:
            continue
        result = _PARSERS[manifest_name](raw)
        if result is not None:
            return result
    return ParsedManifest.empty()


def parse_manifest_at_ingest(jar_bytes: bytes, *, loader: str) -> ParsedManifest:
    """Parse a jar at install time, never raising (issue #1307).

    ``loader`` is the Modrinth family string for the server's type
    (``fabric``/``forge``/``paper``). The install path has already validated the
    ``.jar`` and bounded the upload size, and the loader is known from the server
    type regardless of the manifest -- so a jar that is not a readable zip (an
    :class:`InvalidModJarError`) must not block the install. Such a jar simply
    carries no parsed dependency metadata (empty result).
    """

    try:
        return parse_manifest(jar_bytes, server_type=loader)
    except InvalidModJarError:
        return ParsedManifest.empty()


def _read_entry_chunked(zf: zipfile.ZipFile, name: str) -> str:
    """Read a zip entry in chunks, enforcing the decompressed-size cap.

    Raises :class:`InvalidModJarError` as soon as ``_MAX_DECOMPRESSED_BYTES`` is
    exceeded -- before the full entry is materialised -- so a malicious jar with
    an extreme compression ratio cannot exhaust memory.
    """
    chunks: list[bytes] = []
    total = 0
    with zf.open(name) as f:
        while True:
            chunk = f.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_DECOMPRESSED_BYTES:
                raise InvalidModJarError("decompressed size exceeds limit")
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


# --- per-manifest parsers --------------------------------------------------
# Each returns None when its manifest is garbled or lacks an identity, so the
# caller can fall through to the next descriptor for the family.


def _parse_fabric(raw: str) -> ParsedManifest | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    mod_id = data.get("id")
    if not isinstance(mod_id, str) or not mod_id:
        return None

    depends = _as_dict(data.get("depends"))
    recommends = _as_dict(data.get("recommends"))
    breaks = _as_dict(data.get("breaks"))

    dependencies: list[dict[str, object]] = []
    for dep_id, rng in depends.items():
        if dep_id in _FABRIC_RUNTIME_IDS:
            continue
        dependencies.append(_dep(dep_id, _fabric_range(rng), required=True))
    for dep_id, rng in recommends.items():
        if dep_id in _FABRIC_RUNTIME_IDS:
            continue
        dependencies.append(_dep(dep_id, _fabric_range(rng), required=False))
    for dep_id, rng in breaks.items():
        if dep_id in _FABRIC_RUNTIME_IDS:
            continue
        dependencies.append(
            _dep(dep_id, _fabric_range(rng), required=False, conflict=True)
        )

    return ParsedManifest(
        mod_identifier=mod_id,
        provides=_str_list(data.get("provides")),
        mc_versions=_fabric_mc_versions(depends.get("minecraft")),
        dependencies=dependencies,
    )


def _parse_quilt(raw: str) -> ParsedManifest | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    loader = data.get("quilt_loader") if isinstance(data, dict) else None
    if not isinstance(loader, dict):
        return None
    mod_id = loader.get("id")
    if not isinstance(mod_id, str) or not mod_id:
        return None

    mc_versions: list[str] = []
    dependencies: list[dict[str, object]] = []
    raw_depends = loader.get("depends")
    if isinstance(raw_depends, list):
        for entry in raw_depends:
            if not isinstance(entry, dict):
                continue
            dep_id = entry.get("id")
            if not isinstance(dep_id, str) or not dep_id:
                continue
            rng = _fabric_range(entry.get("versions"))
            if dep_id == "minecraft":
                mc_versions = _fabric_mc_versions(entry.get("versions"))
                continue
            if dep_id in _FABRIC_RUNTIME_IDS:
                continue
            dependencies.append(_dep(dep_id, rng, required=not entry.get("optional")))
    raw_breaks = loader.get("breaks")
    if isinstance(raw_breaks, list):
        for entry in raw_breaks:
            if not isinstance(entry, dict):
                continue
            dep_id = entry.get("id")
            if not isinstance(dep_id, str) or not dep_id:
                continue
            if dep_id in _FABRIC_RUNTIME_IDS:
                continue
            rng = _fabric_range(entry.get("versions"))
            dependencies.append(_dep(dep_id, rng, required=False, conflict=True))

    return ParsedManifest(
        mod_identifier=mod_id,
        provides=_quilt_provides(loader.get("provides")),
        mc_versions=mc_versions,
        dependencies=dependencies,
    )


def _parse_forge_like(raw: str) -> ParsedManifest | None:
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        return None
    mods = data.get("mods")
    if not isinstance(mods, list) or not mods or not isinstance(mods[0], dict):
        return None
    first = mods[0]
    mod_id = first.get("modId")
    if not isinstance(mod_id, str) or not mod_id:
        return None

    mc_versions: list[str] = []
    dependencies: list[dict[str, object]] = []
    deps_section = data.get("dependencies")
    entries = deps_section.get(mod_id) if isinstance(deps_section, dict) else None
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            dep_id = entry.get("modId")
            if not isinstance(dep_id, str) or not dep_id:
                continue
            rng = _str_or_empty(entry.get("versionRange"))
            if dep_id == "minecraft":
                mc_versions = [rng] if rng else []
                continue
            if dep_id in _FORGE_RUNTIME_IDS:
                continue
            if _forge_conflict(entry):
                dependencies.append(_dep(dep_id, rng, required=False, conflict=True))
                continue
            dependencies.append(_dep(dep_id, rng, required=_forge_required(entry)))

    return ParsedManifest(
        mod_identifier=mod_id,
        provides=[],
        mc_versions=mc_versions,
        dependencies=dependencies,
    )


def _parse_paper(raw: str) -> ParsedManifest | None:
    data = _parse_plugin_yaml(raw)
    name = data.get("name")
    if not isinstance(name, str) or not name:
        return None

    dependencies: list[dict[str, object]] = []
    for dep_name in _str_list(data.get("depend")):
        dependencies.append(_dep(dep_name, "", required=True))
    for dep_name in _str_list(data.get("softdepend")):
        dependencies.append(_dep(dep_name, "", required=False))

    api_version = data.get("api-version")
    mc_versions = [api_version] if isinstance(api_version, str) and api_version else []

    return ParsedManifest(
        mod_identifier=name,
        provides=[],
        mc_versions=mc_versions,
        dependencies=dependencies,
    )


_PARSERS = {
    _FABRIC_MANIFEST: _parse_fabric,
    _QUILT_MANIFEST: _parse_quilt,
    _FORGE_MANIFEST: _parse_forge_like,
    _NEOFORGE_MANIFEST: _parse_forge_like,
    _PAPER_MANIFEST: _parse_paper,
    _BUKKIT_MANIFEST: _parse_paper,
}


# --- minimal plugin.yml parsing --------------------------------------------
# A Bukkit/Paper plugin.yml is flat YAML; we extract only the handful of keys we
# need (name, version, api-version, depend, softdepend). A targeted parser avoids
# adding a YAML dependency for these few scalar/list fields.


def _parse_plugin_yaml(raw: str) -> dict[str, object]:
    """Extract top-level scalar and list values from a flat ``plugin.yml``.

    Handles ``key: value`` scalars, ``key: [a, b]`` inline lists, and
    ``key:`` followed by ``  - item`` block lists -- the shapes Bukkit/Paper
    descriptors use for the fields we read. Nested mappings and other YAML
    features are ignored.
    """

    result: dict[str, object] = {}
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Only top-level keys (no leading indentation).
        if line[:1].isspace():
            continue
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest:
            if rest.startswith("[") and rest.endswith("]"):
                result[key] = _parse_inline_list(rest)
            else:
                result[key] = _unquote(rest)
            continue
        # Block list: subsequent indented "- item" lines.
        items: list[str] = []
        while i < len(lines):
            nxt = lines[i]
            nxt_stripped = nxt.strip()
            if nxt_stripped.startswith("- "):
                items.append(_unquote(nxt_stripped[2:].strip()))
                i += 1
            elif nxt_stripped == "" or nxt_stripped.startswith("#"):
                i += 1
            elif nxt[:1].isspace():
                # An indented non-list line: not a list we understand; stop.
                break
            else:
                break
        if items:
            result[key] = items
    return result


def _parse_inline_list(rest: str) -> list[str]:
    inner = rest[1:-1].strip()
    if not inner:
        return []
    return [_unquote(part.strip()) for part in inner.split(",") if part.strip()]


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


# --- small helpers ---------------------------------------------------------


def _dep(
    mod_identifier: str,
    version_range: str,
    *,
    required: bool,
    conflict: bool = False,
) -> dict[str, object]:
    return {
        "mod_identifier": mod_identifier,
        "version_range": version_range,
        "required": required,
        "conflict": conflict,
    }


def _fabric_range(rng: object) -> str:
    """A Fabric/Quilt version range is a string or a list of strings (OR)."""
    if isinstance(rng, str):
        return rng
    if isinstance(rng, list):
        return " || ".join(str(r) for r in rng if isinstance(r, str))
    return ""


def _fabric_mc_versions(rng: object) -> list[str]:
    """Normalize a Fabric/Quilt ``minecraft`` constraint to a list of strings."""
    if isinstance(rng, str):
        return [rng] if rng else []
    if isinstance(rng, list):
        return [r for r in rng if isinstance(r, str) and r]
    return []


def _quilt_provides(provides: object) -> list[str]:
    """Quilt ``provides`` entries are strings or ``{"id": ...}`` objects."""
    if not isinstance(provides, list):
        return []
    out: list[str] = []
    for entry in provides:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("id"), str):
            out.append(entry["id"])
    return out


def _forge_required(entry: dict[str, object]) -> bool:
    """Whether a Forge/NeoForge dependency is required.

    Forge uses ``mandatory = true``; NeoForge replaced it with
    ``type = "required"``. A dependency with neither is treated as required
    (the conservative default -- flag a possibly-missing dep rather than hide it).
    """
    if "mandatory" in entry:
        return bool(entry.get("mandatory"))
    if "type" in entry:
        return entry.get("type") != "optional"
    return True


def _forge_conflict(entry: dict[str, object]) -> bool:
    """Whether a Forge/NeoForge dependency entry declares an incompatibility."""
    return entry.get("type") in _FORGE_CONFLICT_TYPES


def _str_list(value: object) -> list[str]:
    """Coerce a value to a list of non-empty strings (drops non-string items)."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v]
    return []


def _str_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_dict(value: object) -> dict[str, object]:
    """Return ``value`` as a string-keyed dict, or ``{}`` if it is not one."""
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if isinstance(k, str)}
    return {}
