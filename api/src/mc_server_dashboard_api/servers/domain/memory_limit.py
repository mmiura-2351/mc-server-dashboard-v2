"""Pure validation for the per-server memory limit (per-server resources, #705).

Standard-library only, no I/O — kept here so the validation is deterministic and
unit-testable in isolation (TESTING.md Section 4), mirroring the snapshot-cadence
and backup-schedule config-key validators.

This is the data-model + API foundation for epic #704 sub-issue 1: a single knob,
a per-server memory **limit** (the container/process ceiling), stored on the
``Server`` config blob (DATABASE.md Section 7: a per-server resource value is
config, not a dedicated column — the ``resolved_jar_sha256`` precedent). The
control-plane/worker wiring that derives ``-Xmx`` from it and enforces it lives in
later sub-issues (#706+) and does not touch this module.

**Unit: mebibytes (MiB).** The key name carries the unit (``memory_limit_mb``);
MiB is the natural granularity for a container/cgroup memory ceiling.

**Default: unset.** An absent key means "no per-server limit" — the worker driver
later picks a proportionate default via its ``InstanceSpec.MemoryMB == 0`` path,
so existing servers are unaffected and the key is never retro-written.

**Range.** Validation here is only the value's shape/range, not host capacity
(that is the deferred placement sub-issue #710).
"""

from __future__ import annotations

from typing import Any

from mc_server_dashboard_api.servers.domain.errors import (
    InvalidMemoryLimitError,
)

# The key under which a per-server memory limit (in mebibytes) lives on the Server
# config blob (DATABASE.md Section 7: the limit is config, not a dedicated column —
# the ``resolved_jar_sha256`` reserved-key precedent).
MEMORY_LIMIT_CONFIG_KEY = "memory_limit_mb"

# The smallest limit a server may declare, in MiB. A Minecraft server needs real
# heap plus JVM off-heap/native overhead to start at all; 512 MiB is the smallest
# ceiling under which a small vanilla server can run with a modest heap. Below it
# the JVM cannot start usefully, so a sub-floor value is a misconfiguration, not a
# valid request. (The floor is on the *limit*; the derived ``-Xmx`` sits safely
# below it — that derivation is #706, not here.)
MEMORY_LIMIT_FLOOR_MB = 512

# The largest limit a server may declare, in MiB (1 TiB). This is not a capacity
# check (host capacity is the deferred placement sub-issue #710) — it only rejects
# absurd values (a fat-fingered byte/MiB confusion) that are plainly a typo rather
# than an intent.
MEMORY_LIMIT_CEILING_MB = 1024 * 1024


def memory_limit_from_config(config: dict[str, Any]) -> int | None:
    """Read and validate the per-server memory limit (MiB) from a config blob.

    Returns the limit in MiB when present and valid, ``None`` when absent (no
    per-server limit). A present value must be a positive integer (``bool``
    rejected) within ``[MEMORY_LIMIT_FLOOR_MB, MEMORY_LIMIT_CEILING_MB]``;
    anything else raises :class:`InvalidMemoryLimitError`.
    """

    if MEMORY_LIMIT_CONFIG_KEY not in config:
        return None
    value = config[MEMORY_LIMIT_CONFIG_KEY]
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidMemoryLimitError(MEMORY_LIMIT_CONFIG_KEY)
    if value < MEMORY_LIMIT_FLOOR_MB or value > MEMORY_LIMIT_CEILING_MB:
        raise InvalidMemoryLimitError(str(value))
    return value
