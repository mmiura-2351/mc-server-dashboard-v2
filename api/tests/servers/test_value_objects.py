"""Value-object invariants for the servers domain."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.domain.errors import InvalidServerNameError
from mc_server_dashboard_api.servers.domain.value_objects import (
    DesiredState,
    ObservedState,
    ServerName,
    ServerType,
)


def test_server_name_trims_whitespace() -> None:
    assert ServerName("  survival  ").value == "survival"


def test_server_name_rejects_blank() -> None:
    with pytest.raises(InvalidServerNameError):
        ServerName("   ")


# A server name is shown in every list and names its export's file, so it carries
# no control character, and no lone surrogate, which has no UTF-8 encoding and so
# could not be stored (issue #3957).
@pytest.mark.parametrize(
    "name",
    [
        pytest.param("a\x00b", id="nul"),
        pytest.param("a\nb", id="lf"),
        pytest.param("a\tb", id="tab"),
        pytest.param("a\x1b[2Jb", id="esc"),
        pytest.param("a\x7fb", id="del"),
        pytest.param("a\x85b", id="c1"),
        pytest.param("a\ud800b", id="lone-surrogate"),
    ],
)
def test_server_name_rejects_a_control_character_or_lone_surrogate(name: str) -> None:
    with pytest.raises(InvalidServerNameError) as excinfo:
        ServerName(name)
    # The message names the rule, not the refused name.
    assert name not in str(excinfo.value)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("サバイバル\u3000二", id="japanese"),
        pytest.param("\U0001f468\u200d\U0001f469 a\u202eb", id="zwj-and-bidi"),
    ],
)
def test_server_name_accepts_any_other_character(name: str) -> None:
    assert ServerName(name).value == name


# The migration that pins the live ``ck_server_type`` CHECK. Update this pointer
# when a later migration re-widens the enum.
_SERVER_TYPE_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0025_remove_server_type_spigot.py"
)


def _migration_check_values() -> set[str]:
    """The ``server_type`` values the latest migration's CHECK admits.

    Loads migration 0025 and parses its ``_NEW_CHECK`` ``IN (...)`` clause so the
    enum is pinned to what the database actually enforces -- enum-vs-migration
    drift (the #267 bug class) then fails this test instead of only a 500 against
    a real database.
    """

    spec = importlib.util.spec_from_file_location(
        "_server_type_migration_0025", _SERVER_TYPE_MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(re.findall(r"'([^']+)'", module._NEW_CHECK))


def test_server_type_values_match_database_check_enum() -> None:
    documented = {"vanilla", "paper", "fabric", "forge"}
    assert {t.value for t in ServerType} == documented
    # And the migration's CHECK must admit exactly that set: the enum and the
    # live schema constraint can never silently diverge (issue #267).
    assert _migration_check_values() == documented


def test_desired_state_values_match_database_check_enum() -> None:
    assert {s.value for s in DesiredState} == {"running", "stopped"}


def test_observed_state_values_mirror_the_wire_enum() -> None:
    # The persisted vocabulary equals the control-plane ``ServerState`` vocabulary
    # (CONTROL_PLANE.md Section 6). ``unknown`` joined the wire enum in issue #2474,
    # so it is no longer an API-only inference.
    assert {s.value for s in ObservedState} == {
        "starting",
        "running",
        "stopping",
        "stopped",
        "restarting",
        "crashed",
        "unknown",
    }
