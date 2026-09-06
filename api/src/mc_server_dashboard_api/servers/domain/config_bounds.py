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
  in PR #148, so any ``null`` (at any depth) is rejected;
- a no-lone-surrogate rule: a JSON string may escape an unpaired surrogate
  (``"\\ud800"``), which Python decodes into a code point that no UTF-8 encoder
  accepts — so such a value can neither be sized here nor stored in the column, and
  is rejected outright rather than blowing up mid-write (issue #2838).

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


class ConfigLoneSurrogateError(ServerError):
    """The ``config`` contains an unpaired surrogate code point (at any depth)."""


def validate_config(config: Any) -> dict[str, Any]:
    """Validate a client-supplied config blob, returning it unchanged if sound.

    Raises :class:`ConfigInvalidShapeError` when the top level is not an object or
    the structure nests beyond :data:`MAX_CONFIG_DEPTH`,
    :class:`ConfigNullValueError` when any value is ``null``,
    :class:`ConfigLoneSurrogateError` when any key or value carries an unpaired
    surrogate, and :class:`ConfigTooLargeError` when its JSON serialization exceeds
    :data:`MAX_CONFIG_BYTES`.

    The surrogate rule is checked before the size ceiling, so a blob that breaks
    both is refused as a lone surrogate: text that no UTF-8 encoder accepts has no
    serialized size to compare against the ceiling in the first place.
    """

    if not isinstance(config, dict):
        raise ConfigInvalidShapeError("config must be a JSON object")
    if _depth(config) > MAX_CONFIG_DEPTH:
        raise ConfigInvalidShapeError("config nests too deeply")
    if _has_null(config):
        raise ConfigNullValueError("config may not contain a null value")
    if _has_lone_surrogate(config):
        raise ConfigLoneSurrogateError("config may not contain an unpaired surrogate")
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


def _has_lone_surrogate(value: Any) -> bool:
    """True if any string in ``value`` carries a surrogate code point.

    Tested by code point rather than by catching the encoder, so the refusal names
    this condition and cannot absorb an unrelated failure. Keys are inspected as
    well as values: a JSON object key can carry the escape just as a value can.

    Every surrogate reaching here is unpaired — a well-formed escape pair is
    already one astral character by the time ``json`` has decoded the request body,
    and Python strings hold code points, not UTF-16 units.
    """

    if isinstance(value, str):
        return any("\ud800" <= char <= "\udfff" for char in value)
    if isinstance(value, dict):
        return any(
            _has_lone_surrogate(k) or _has_lone_surrogate(v) for k, v in value.items()
        )
    if isinstance(value, list):
        return any(_has_lone_surrogate(v) for v in value)
    return False


def _depth(value: Any) -> int:
    """Maximum nesting depth of a JSON-like value (a scalar is depth 1)."""

    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(v) for v in value), default=0)
    return 1
