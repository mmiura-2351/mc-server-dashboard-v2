"""Proto-mapping tests for the data-plane trigger commands (issue #106).

The fleet adapter must translate the domain :class:`HydrateCommand` /
:class:`SnapshotCommand` into the wire ``HydrateTrigger`` / ``SnapshotTrigger``
payloads carrying the transfer URL + token (CONTROL_PLANE.md Section 5).
"""

from __future__ import annotations

from mc_server_dashboard_api.fleet.adapters.control_plane import _to_api_command
from mc_server_dashboard_api.fleet.domain.control_plane import (
    HydrateCommand,
    SnapshotCommand,
    StopServerCommand,
)


def test_hydrate_command_maps_to_hydrate_trigger() -> None:
    api = _to_api_command(
        "cmd-1",
        "server-1",
        HydrateCommand(transfer_url="https://api/x/working-set", transfer_token="t"),
    )
    assert api.WhichOneof("command") == "hydrate"
    assert api.hydrate.transfer_url == "https://api/x/working-set"
    assert api.hydrate.transfer_token == "t"


def test_snapshot_command_maps_to_snapshot_trigger() -> None:
    api = _to_api_command(
        "cmd-2",
        "server-2",
        SnapshotCommand(transfer_url="https://api/x/snapshot", transfer_token="t"),
    )
    assert api.WhichOneof("command") == "snapshot"
    assert api.snapshot.transfer_url == "https://api/x/snapshot"
    assert api.snapshot.transfer_token == "t"


def test_stop_command_carries_force_in_proto() -> None:
    # The force flag must reach the wire StopServer message so the Worker takes
    # the immediate-kill path (issue #270).
    api = _to_api_command("cmd-3", "server-3", StopServerCommand(force=True))
    assert api.WhichOneof("command") == "stop"
    assert api.stop.force is True


def test_stop_command_defaults_force_false_in_proto() -> None:
    api = _to_api_command("cmd-4", "server-4", StopServerCommand())
    assert api.WhichOneof("command") == "stop"
    assert api.stop.force is False
