"""Model-rendered DDL for the ``server`` table (DATABASE.md Section 7).

Verifies the table carries the documented constraints and that
``assigned_worker_id`` is FK-free (the ``worker`` table is not yet persisted).
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from mc_server_dashboard_api.servers.adapters.models import ServerModel

_TABLE = cast(Table, ServerModel.__table__)


def _check_constraint(name: str) -> CheckConstraint:
    for constraint in _TABLE.constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == name:
            return constraint
    raise AssertionError(f"missing CHECK constraint {name!r}")


def test_unique_community_name() -> None:
    names = {c.name for c in _TABLE.constraints if isinstance(c, UniqueConstraint)}
    assert "uq_server_community_name" in names


def test_game_port_is_nullable_and_unique() -> None:
    # The tracked game port is nullable (legacy rows have none) and unique
    # deployment-wide (issue #243).
    assert _TABLE.c.game_port.nullable is True
    names = {c.name for c in _TABLE.constraints if isinstance(c, UniqueConstraint)}
    assert "uq_server_game_port" in names


def test_check_constraints_present() -> None:
    for name in (
        "ck_server_type",
        "ck_server_desired_state",
        "ck_server_observed_state",
    ):
        assert _check_constraint(name) is not None


def test_assigned_worker_id_has_no_foreign_key() -> None:
    # The worker table is not yet persisted; the FK lands when it does.
    assert _TABLE.c.assigned_worker_id.foreign_keys == set()
    assert _TABLE.c.assigned_worker_id.nullable is True


def test_community_id_cascade_delete() -> None:
    fks = list(_TABLE.c.community_id.foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "CASCADE"
