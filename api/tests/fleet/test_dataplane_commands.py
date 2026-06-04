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
