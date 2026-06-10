"""Pure scheduling math for per-server scheduled backups (FR-BAK-3).

Standard-library only, no I/O and no clock — kept here so the schedule decisions
are deterministic and unit-testable in isolation (TESTING.md Section 4). The
backup scheduler use case combines these with the live clock and an in-memory
due-tracking map (the snapshot-cadence pattern).

**Schedule format (M1).** A per-server *interval in hours* stored on the
``Server`` config blob (DATABASE.md Section 8 places the schedule in
``server.config``), absent meaning "no scheduled backups". This is chosen over a
cron expression at M1 because it needs no cron parser/dependency, validates
exactly like the existing ``snapshot_interval_seconds`` override (FR-DATA-7), and
an interval is the natural granularity for backup cadence. A cron-style schedule
is a follow-on behind epic #649.

Per-server jitter spreads the due instants so a fleet sharing one interval does
not back up in lockstep; it is derived deterministically from the server id, so
the same server always gets the same offset, bounded by a small fraction of the
interval (the snapshot-cadence thundering-herd guard).
"""

from __future__ import annotations

import hashlib
from typing import Any

from mc_server_dashboard_api.servers.domain.errors import (
    InvalidBackupScheduleError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

# The jitter is at most this fraction of the effective interval, so it staggers
# the herd without meaningfully changing the cadence each server actually gets.
JITTER_FRACTION = 0.1

# The key under which a per-server backup-schedule interval (in hours) lives on
# the Server config blob (DATABASE.md Section 8: the schedule is config, not a
# dedicated table — a single per-server schedule needs no ``backup_schedule`` row).
BACKUP_INTERVAL_CONFIG_KEY = "backup_interval_hours"

_SECONDS_PER_HOUR = 3600


def schedule_from_config(config: dict[str, Any]) -> int | None:
    """Read and validate the per-server backup interval (hours) from config.

    Returns the interval in *seconds* when a valid schedule is present, ``None``
    when absent (no scheduled backups). A present value must be a positive integer
    number of hours (``bool`` rejected); anything else raises
    :class:`InvalidBackupScheduleError`. The same validation runs on the write path
    (the update use case, so a bad schedule 422s) and the read path (the scheduler,
    defensively).
    """

    if BACKUP_INTERVAL_CONFIG_KEY not in config:
        return None
    value = config[BACKUP_INTERVAL_CONFIG_KEY]
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidBackupScheduleError(BACKUP_INTERVAL_CONFIG_KEY)
    if value < 1:
        raise InvalidBackupScheduleError(str(value))
    return value * _SECONDS_PER_HOUR


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
