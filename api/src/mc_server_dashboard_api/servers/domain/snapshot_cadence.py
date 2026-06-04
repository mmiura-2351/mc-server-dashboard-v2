"""Pure scheduling math for periodic snapshot cadence (FR-DATA-7).

Standard-library only, no I/O and no clock — kept here so the cadence decisions
are deterministic and unit-testable in isolation (TESTING.md Section 4). The
scheduler use case combines these with the live clock and an in-memory
due-tracking map.

The effective periodic interval for a running server is its per-server override
(stored on the ``Server`` config blob, DATABASE.md Section 7) if set, otherwise
the global default; both are clamped up to ``min_interval_seconds`` so the floor
is an absolute guarantee against snapshot thrash (CONFIGURATION.md Section 5.4).

Per-server jitter spreads the due instants so a fleet of servers sharing one
interval does not snapshot in lockstep (the thundering-herd guard). It is derived
deterministically from the server id, so the same server always gets the same
offset, and bounded by a small fraction of the interval.
"""

from __future__ import annotations

import hashlib
from typing import Any

from mc_server_dashboard_api.servers.domain.errors import (
    InvalidSnapshotIntervalError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

# The jitter is at most this fraction of the effective interval, so it staggers
# the herd without meaningfully changing the RPO each server actually gets.
JITTER_FRACTION = 0.1

# The key under which a per-server interval override lives on the Server config
# blob (DATABASE.md Section 7: the override is config, not a dedicated column).
SNAPSHOT_INTERVAL_CONFIG_KEY = "snapshot_interval_seconds"


def override_from_config(config: dict[str, Any], *, floor: int) -> int | None:
    """Read and validate the per-server interval override from a config blob.

    Returns the override seconds when present and valid, ``None`` when absent. A
    present value must be a positive integer (``bool`` rejected) at least
    ``floor``; anything else raises :class:`InvalidSnapshotIntervalError`. The
    same validation is applied on the write path (the update use case, so a bad
    override 422s) and the read path (the scheduler, defensively).
    """

    if SNAPSHOT_INTERVAL_CONFIG_KEY not in config:
        return None
    value = config[SNAPSHOT_INTERVAL_CONFIG_KEY]
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidSnapshotIntervalError(SNAPSHOT_INTERVAL_CONFIG_KEY)
    if value < floor:
        raise InvalidSnapshotIntervalError(str(value))
    return value


def effective_interval_seconds(
    *, override: int | None, default: int, floor: int
) -> int:
    """Return the periodic interval to apply, clamped to at least ``floor``.

    ``override`` (the per-server value) replaces ``default`` when set; the result
    is clamped up to ``floor`` so neither a low override nor a misconfigured
    default can drive snapshots below the thrash floor.
    """

    chosen = default if override is None else override
    return max(chosen, floor)


def jitter_seconds(server_id: ServerId, *, interval_seconds: int) -> float:
    """Return a deterministic per-server offset in ``[0, interval * fraction)``.

    Derived from the server id via a stable hash, so it survives restarts and
    differs across servers, spreading the due instants of servers that share an
    interval (the thundering-herd guard).
    """

    digest = hashlib.sha256(server_id.value.bytes).digest()
    # Map the first 8 digest bytes to a fraction in [0, 1), then scale to the bound.
    fraction = int.from_bytes(digest[:8], "big") / 2**64
    return fraction * interval_seconds * JITTER_FRACTION
