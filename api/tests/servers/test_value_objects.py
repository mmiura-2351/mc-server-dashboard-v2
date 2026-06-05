"""Value-object invariants for the servers domain."""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.servers.domain.errors import InvalidServerNameError
from mc_server_dashboard_api.servers.domain.value_objects import (
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerName,
    ServerType,
)


def test_server_name_trims_whitespace() -> None:
    assert ServerName("  survival  ").value == "survival"


def test_server_name_rejects_blank() -> None:
    with pytest.raises(InvalidServerNameError):
        ServerName("   ")


def test_execution_backend_values_match_database_check_enum() -> None:
    # DATABASE.md Section 7 spells the backend with underscores.
    assert {b.value for b in ExecutionBackend} == {"host_process", "container"}


def test_server_type_values_match_database_check_enum() -> None:
    assert {t.value for t in ServerType} == {
        "vanilla",
        "paper",
        "fabric",
        "forge",
        "spigot",
    }


def test_desired_state_values_match_database_check_enum() -> None:
    assert {s.value for s in DesiredState} == {"running", "stopped"}


def test_observed_state_includes_api_inferred_unknown() -> None:
    # The reportable values plus the API-inferred ``unknown`` (CONTROL_PLANE.md 6).
    assert {s.value for s in ObservedState} == {
        "starting",
        "running",
        "stopping",
        "stopped",
        "restarting",
        "crashed",
        "unknown",
    }
