"""Pure validation for the per-server CPU allocation (per-server resources, #722).

Standard-library only, no I/O — kept here so the validation is deterministic and
unit-testable in isolation (TESTING.md Section 4), mirroring the per-server
memory-limit validator (``memory_limit.py``, #705).

This is the data-model + API foundation for epic #704's CPU dimension: a single
knob, a per-server CPU **allocation**, stored on the ``Server`` config blob
(DATABASE.md Section 7: a per-server resource value is config, not a dedicated
column — the ``memory_limit_mb`` precedent). The control-plane/worker wiring that
carries it into ``InstanceSpec`` and the driver enforcement that translates it to
a container ``CPUShares`` relative weight live in later sub-issues (#723+) and do
not touch this module.

**Soft allocation, not a hard cap.** Unlike the memory limit (a container/cgroup
ceiling), the CPU value is a *soft, rough allocation* — a relative share of the
host's CPU (owner decision 2026-06-09). It expresses roughly how much CPU a server
should get under contention; it is NOT a strict quota and a server may burst above
it when the host is idle. The enforcement mechanism (later sub-issues) is a
relative weight (Docker ``CPUShares``), not a hard limit (``NanoCPUs``).

**Unit: millicores.** ``1000 = one core``, matching the codebase's existing
``cpu_millis`` runtime-metric convention (proto ``ServerStats.cpu_millis``). The
key name carries the unit (``cpu_millis``).

**Default: unset.** An absent key means "no per-server CPU allocation" — the
server runs with the driver's default share, so existing servers are unaffected
and the key is never retro-written.

**Range.** Validation here is only the value's shape/range, not host capacity
(that is the deferred placement sub-issue #710). The bounds are deliberately loose
(this is a rough allocation, not precise admission control): a floor that keeps a
server's main tick thread able to make progress, and a generous ceiling that only
rejects plainly absurd/typo values.
"""

from __future__ import annotations

from typing import Any

from mc_server_dashboard_api.servers.domain.errors import (
    InvalidCpuAllocationError,
)

# The key under which a per-server CPU allocation (in millicores) lives on the
# Server config blob (DATABASE.md Section 7: the allocation is config, not a
# dedicated column — the ``memory_limit_mb`` reserved-key precedent).
CPU_ALLOCATION_CONFIG_KEY = "cpu_millis"

# The smallest allocation a server may declare, in millicores (~0.1 core). Below
# this a Minecraft server's single-threaded main tick loop cannot make meaningful
# progress, so a sub-floor value is a misconfiguration rather than a valid intent.
# This is a loose sanity floor, not precise admission control.
CPU_ALLOCATION_FLOOR_MILLIS = 100

# The largest allocation a server may declare, in millicores (128 cores). This is
# not a capacity check (host capacity is the deferred placement sub-issue #710) —
# it only rejects plainly absurd values (a fat-fingered unit confusion) sized well
# above any realistic single-host core count.
CPU_ALLOCATION_CEILING_MILLIS = 128_000


def cpu_allocation_from_config(config: dict[str, Any]) -> int | None:
    """Read and validate the per-server CPU allocation (millicores) from a config blob.

    Returns the allocation in millicores when present and valid, ``None`` when
    absent (no per-server allocation). A present value must be a positive integer
    (``bool`` rejected) within
    ``[CPU_ALLOCATION_FLOOR_MILLIS, CPU_ALLOCATION_CEILING_MILLIS]``; anything else
    raises :class:`InvalidCpuAllocationError`.
    """

    if CPU_ALLOCATION_CONFIG_KEY not in config:
        return None
    value = config[CPU_ALLOCATION_CONFIG_KEY]
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCpuAllocationError(CPU_ALLOCATION_CONFIG_KEY)
    if value < CPU_ALLOCATION_FLOOR_MILLIS or value > CPU_ALLOCATION_CEILING_MILLIS:
        raise InvalidCpuAllocationError(str(value))
    return value
