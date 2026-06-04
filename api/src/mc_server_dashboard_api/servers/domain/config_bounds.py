"""Proportionate bounds for the client-supplied server ``config`` blob.

The config column stores ``server.properties``-style key/value settings (plus a
few per-server overrides, DATABASE.md Section 7). Without a guard a client could
write megabytes of arbitrary JSON into the row, so create/update apply two cheap,
standard-library-only checks before staging:

- a serialized-size ceiling (:data:`MAX_CONFIG_BYTES`), generous for any real
  server configuration but small enough that the column cannot be abused as bulk
  storage;
- a shape sanity rule: the top level must be a JSON object, and the structure may
  not nest deeper than :data:`MAX_CONFIG_DEPTH` — a flat-ish settings blob never
  approaches that, while a pathologically nested payload is rejected;
- a no-null rule: a JSON ``null`` is never a meaningful ``server.properties``-style
  value, and a null value is the shape that enabled the key-presence smuggle fixed
  in PR #148, so any ``null`` (at any depth) is rejected.

Pure (no I/O, no framework types) so the bound is deterministic and unit-testable
in isolation (TESTING.md Section 4). The edge maps the errors to a typed 422.
"""

from __future__ import annotations

import json
from typing import Any

from mc_server_dashboard_api.servers.domain.errors import ServerError

# 64 KiB: a real server.properties-style config is a few KiB at most, so this is
# generous headroom while still bounding the row against bulk-storage abuse.
MAX_CONFIG_BYTES = 64 * 1024

# A flat-ish settings blob nests one or two levels; 8 is comfortable headroom and
# the recursive check below is cheap (linear in the node count, bounded depth).
MAX_CONFIG_DEPTH = 8


class ConfigTooLargeError(ServerError):
    """The serialized ``config`` exceeds :data:`MAX_CONFIG_BYTES`."""


class ConfigInvalidShapeError(ServerError):
    """The ``config`` is not a top-level object or nests beyond the depth cap."""


class ConfigNullValueError(ServerError):
    """The ``config`` contains a JSON ``null`` value (at any depth)."""


def validate_config(config: Any) -> dict[str, Any]:
    """Validate a client-supplied config blob, returning it unchanged if sound.

    Raises :class:`ConfigInvalidShapeError` when the top level is not an object or
    the structure nests beyond :data:`MAX_CONFIG_DEPTH`,
    :class:`ConfigNullValueError` when any value is ``null``, and
    :class:`ConfigTooLargeError` when its JSON serialization exceeds
    :data:`MAX_CONFIG_BYTES`.
    """

    if not isinstance(config, dict):
        raise ConfigInvalidShapeError("config must be a JSON object")
    if _depth(config) > MAX_CONFIG_DEPTH:
        raise ConfigInvalidShapeError("config nests too deeply")
    if _has_null(config):
        raise ConfigNullValueError("config may not contain a null value")
    # ``ensure_ascii=False`` so multibyte values are sized by their real UTF-8
    # byte length rather than escaped ASCII, matching what the column stores.
    size = len(json.dumps(config, ensure_ascii=False).encode("utf-8"))
    if size > MAX_CONFIG_BYTES:
        raise ConfigTooLargeError("config exceeds the size limit")
    return config


def _has_null(value: Any) -> bool:
    """True if ``value`` is ``None`` or contains a ``None`` at any depth."""

    if value is None:
        return True
    if isinstance(value, dict):
        return any(_has_null(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_null(v) for v in value)
    return False


def _depth(value: Any) -> int:
    """Maximum nesting depth of a JSON-like value (a scalar is depth 1)."""

    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(v) for v in value), default=0)
    return 1
