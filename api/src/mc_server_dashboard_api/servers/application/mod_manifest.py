"""Parse a mod jar's embedded manifest into structured metadata (issue #1260).

A mod ``.jar`` is a zip that carries a loader-specific descriptor at a known
path. This module reads that descriptor and returns a uniform
:class:`ParsedModMetadata` — the single metadata source for both manual uploads
and (later) Modrinth imports. It is pure parsing: no storage, no DB, no network.

Loaders and their manifests:

============  ==============================  =================================
Loader        File                            Format
============  ==============================  =================================
Fabric        ``fabric.mod.json``             JSON (root)
Quilt         ``quilt.mod.json``              JSON (``quilt_loader.*``)
Forge         ``META-INF/mods.toml``          TOML
NeoForge      ``META-INF/neoforge.mods.toml`` TOML
Paper/Bukkit  ``paper-plugin.yml``/``plugin.yml``  YAML
============  ==============================  =================================

Side auto-detection is loader-specific: Fabric ``environment`` maps
``client``/``server``/``*`` -> the corresponding :data:`ModSide`; Paper/Bukkit
plugins are always ``server``; Forge/NeoForge side hints are unreliable so they
default to ``both``. ``both`` is the safe default whenever the side is
undetectable (a ``both`` mod is present everywhere).

Robustness contract:

* Bytes that do not open as a zip, carry too many entries, or decompress past
  the size cap raise :class:`InvalidModJarError` — the jar is unusable.
* A readable zip with no recognized manifest, or a recognized manifest that is
  garbled or missing its identity, returns :meth:`ParsedModMetadata.unknown`
  (``loader_type="unknown"``) rather than raising — the caller can still store
  the jar and let the user fill in metadata manually (epic #1258).

When a jar carries more than one recognized manifest, loaders are tried in
:data:`_LOADER_ORDER` (modloaders before the legacy plugin descriptor) and the
first that yields a usable result wins.
"""

from __future__ import annotations

import io
import json
import tomllib
import zipfile
from dataclasses import dataclass, field
from typing import Literal

import yaml

from mc_server_dashboard_api.servers.domain.errors import InvalidModJarError
from mc_server_dashboard_api.servers.domain.mod import ModLoader, ModSide

# Safety limits (resource_pack_zip.py precedent, #1252/#1254 hardening).
_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024  # 256 MiB (mod upload cap, epic #1258)
_MAX_ENTRY_COUNT = 50_000
_CHUNK_SIZE = 64 * 1024  # 64 KiB

# Manifest paths per loader (case-sensitive, matched against the zip entry name).
_FABRIC_MANIFEST = "fabric.mod.json"
_QUILT_MANIFEST = "quilt.mod.json"
_FORGE_MANIFEST = "META-INF/mods.toml"
_NEOFORGE_MANIFEST = "META-INF/neoforge.mods.toml"
_PAPER_MANIFEST = "paper-plugin.yml"
_BUKKIT_MANIFEST = "plugin.yml"

# The detected loader, or "unknown" when no manifest was recognized.
ParsedLoader = Literal["fabric", "forge", "neoforge", "quilt", "paper", "unknown"]


@dataclass(frozen=True)
class ParsedModMetadata:
    """Structured metadata extracted from a mod jar manifest.

    ``loader_type`` is the detected loader, or ``"unknown"`` when no manifest was
    recognized. ``dependencies`` use the library's persisted shape:
    ``[{"mod_identifier": str, "version_range": str, "required": bool}]``.
    ``side`` defaults to ``"both"`` whenever undetectable.
    """

    loader_type: ParsedLoader
    mod_identifier: str
    provides: list[str] = field(default_factory=list)
    version_number: str = ""
    mc_versions: list[str] = field(default_factory=list)
    dependencies: list[dict[str, object]] = field(default_factory=list)
    side: ModSide = "both"

    @classmethod
    def unknown(cls) -> ParsedModMetadata:
        """The result for a readable jar with no usable manifest."""

        return cls(loader_type="unknown", mod_identifier="")


def parse_manifest(jar_bytes: bytes) -> ParsedModMetadata:
    """Parse a mod jar's manifest into :class:`ParsedModMetadata`.

    Raises :class:`InvalidModJarError` if the bytes are not a readable, bounded
    zip. Returns :meth:`ParsedModMetadata.unknown` if the zip carries no usable
    manifest.
    """
    if not zipfile.is_zipfile(io.BytesIO(jar_bytes)):
        raise InvalidModJarError("not a valid jar (zip)")

    names: dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(jar_bytes)) as zf:
            infos = zf.infolist()
            if len(infos) > _MAX_ENTRY_COUNT:
                raise InvalidModJarError("too many entries")
            wanted = {
                _FABRIC_MANIFEST,
                _QUILT_MANIFEST,
                _FORGE_MANIFEST,
                _NEOFORGE_MANIFEST,
                _PAPER_MANIFEST,
                _BUKKIT_MANIFEST,
            }
            for info in infos:
                if info.is_dir() or info.filename not in wanted:
                    continue
                names[info.filename] = _read_entry_chunked(zf, info.filename)
    except zipfile.BadZipFile as exc:
        raise InvalidModJarError("corrupt jar (zip)") from exc

    for parser in _LOADER_ORDER:
        result = parser(names)
        if result is not None:
            return result
    return ParsedModMetadata.unknown()


def _read_entry_chunked(zf: zipfile.ZipFile, name: str) -> str:
    """Read a zip entry in chunks, enforcing the decompressed-size cap.

    Raises :class:`InvalidModJarError` as soon as ``_MAX_DECOMPRESSED_BYTES`` is
    exceeded — before the full entry is materialised — so a malicious jar with
    an extreme compression ratio cannot exhaust memory (#1254).
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


# --- per-loader parsers ----------------------------------------------------
# Each returns None when its manifest is absent, garbled, or lacks an identity,
# so the caller can fall through to the next loader.

# Fabric/Quilt ids that are loader/runtime constraints, not real mod deps.
_FABRIC_RUNTIME_IDS = frozenset({"minecraft", "java", "fabricloader", "fabric"})
# Forge/NeoForge dependency ids that are loader/runtime constraints.
_FORGE_RUNTIME_IDS = frozenset({"minecraft", "forge", "neoforge"})


def _parse_fabric(names: dict[str, str]) -> ParsedModMetadata | None:
    raw = names.get(_FABRIC_MANIFEST)
    if raw is None:
        return None
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

    dependencies: list[dict[str, object]] = []
    for dep_id, rng in depends.items():
        if dep_id in _FABRIC_RUNTIME_IDS:
            continue
        dependencies.append(_dep(dep_id, _fabric_range(rng), required=True))
    for dep_id, rng in recommends.items():
        if dep_id in _FABRIC_RUNTIME_IDS:
            continue
        dependencies.append(_dep(dep_id, _fabric_range(rng), required=False))

    return ParsedModMetadata(
        loader_type="fabric",
        mod_identifier=mod_id,
        provides=_str_list(data.get("provides")),
        version_number=_str_or_empty(data.get("version")),
        mc_versions=_fabric_mc_versions(depends.get("minecraft")),
        dependencies=dependencies,
        side=_fabric_side(data.get("environment")),
    )


def _parse_quilt(names: dict[str, str]) -> ParsedModMetadata | None:
    raw = names.get(_QUILT_MANIFEST)
    if raw is None:
        return None
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

    return ParsedModMetadata(
        loader_type="quilt",
        mod_identifier=mod_id,
        provides=_quilt_provides(loader.get("provides")),
        version_number=_str_or_empty(loader.get("version")),
        mc_versions=mc_versions,
        dependencies=dependencies,
        side="both",
    )


def _parse_forge(names: dict[str, str]) -> ParsedModMetadata | None:
    return _parse_forge_like(names.get(_FORGE_MANIFEST), "forge")


def _parse_neoforge(names: dict[str, str]) -> ParsedModMetadata | None:
    return _parse_forge_like(names.get(_NEOFORGE_MANIFEST), "neoforge")


def _parse_forge_like(raw: str | None, loader: ModLoader) -> ParsedModMetadata | None:
    if raw is None:
        return None
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
            dependencies.append(_dep(dep_id, rng, required=_forge_required(entry)))

    return ParsedModMetadata(
        loader_type=loader,
        mod_identifier=mod_id,
        provides=[],
        version_number=_str_or_empty(first.get("version")),
        mc_versions=mc_versions,
        dependencies=dependencies,
        # Forge/NeoForge side hints are unreliable -> default both (epic #1258).
        side="both",
    )


def _parse_paper(names: dict[str, str]) -> ParsedModMetadata | None:
    raw = names.get(_PAPER_MANIFEST)
    if raw is None:
        raw = names.get(_BUKKIT_MANIFEST)
    if raw is None:
        return None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
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

    return ParsedModMetadata(
        loader_type="paper",
        mod_identifier=name,
        provides=[],
        version_number=_str_or_empty(data.get("version")),
        mc_versions=mc_versions,
        dependencies=dependencies,
        # A Bukkit/Paper plugin only ever runs server-side.
        side="server",
    )


# Modloaders are tried before the legacy plugin descriptor: a jar carrying both
# a modloader manifest and a plugin.yml is a mod, not a plugin.
_LOADER_ORDER = (
    _parse_fabric,
    _parse_quilt,
    _parse_neoforge,
    _parse_forge,
    _parse_paper,
)


# --- small helpers ---------------------------------------------------------


def _dep(
    mod_identifier: str, version_range: str, *, required: bool
) -> dict[str, object]:
    return {
        "mod_identifier": mod_identifier,
        "version_range": version_range,
        "required": required,
    }


def _fabric_side(environment: object) -> ModSide:
    """Map a Fabric ``environment`` value to a :data:`ModSide`."""
    if environment == "client":
        return "client"
    if environment == "server":
        return "server"
    # "*", missing, or anything unrecognized -> safe default.
    return "both"


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
    (the conservative default — flag a possibly-missing dep rather than hide it).
    """
    if "mandatory" in entry:
        return bool(entry.get("mandatory"))
    if "type" in entry:
        return entry.get("type") != "optional"
    return True


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
