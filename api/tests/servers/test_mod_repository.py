"""Port-contract tests for the mod repository against the in-memory fake.

Exercises the ``ModRepository`` Port behaviour (add / get_by_id / get_by_sha256 /
list_all filters / delete) through ``FakeModRepository``, the harness the future
mod-library use-case tests run against (TESTING.md Section 4). The real
async-SQLAlchemy adapter is covered separately by the DDL/migration tests.
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.servers.domain.mod import Mod, ModId
from mc_server_dashboard_api.servers.domain.server_mod import (
    ServerModAssignment,
    ServerModId,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from tests.servers.fakes import FakeModRepository


def _make_mod(**overrides: object) -> Mod:
    now = dt.datetime.now(dt.timezone.utc)
    defaults: dict[str, object] = {
        "id": ModId.new(),
        "filename": "sodium.jar",
        "display_name": "Sodium",
        "description": None,
        "loader_type": "fabric",
        "mod_identifier": "sodium",
        "provides": [],
        "version_number": "0.5.0",
        "mc_versions": ["1.21"],
        "side": "both",
        "dependencies": [],
        "sha256_hash": "a" * 64,
        "sha512_hash": None,
        "size_bytes": 2048,
        "source": "local",
        "source_project_id": None,
        "source_version_id": None,
        "uploaded_by": uuid.uuid4(),
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return Mod(**defaults)  # type: ignore[arg-type]


async def test_add_then_get_by_id_round_trips() -> None:
    repo = FakeModRepository()
    mod = _make_mod()
    await repo.add(mod)
    assert await repo.get_by_id(mod.id) is mod


async def test_get_by_id_absent_returns_none() -> None:
    repo = FakeModRepository()
    assert await repo.get_by_id(ModId.new()) is None


async def test_get_by_sha256_finds_existing() -> None:
    repo = FakeModRepository()
    mod = _make_mod(sha256_hash="c" * 64)
    await repo.add(mod)
    assert await repo.get_by_sha256("c" * 64) is mod


async def test_get_by_sha256_absent_returns_none() -> None:
    repo = FakeModRepository()
    await repo.add(_make_mod(sha256_hash="a" * 64))
    assert await repo.get_by_sha256("d" * 64) is None


async def test_list_all_orders_by_display_name() -> None:
    repo = FakeModRepository()
    await repo.add(_make_mod(display_name="Zeta", sha256_hash="1" * 64))
    await repo.add(_make_mod(display_name="Alpha", sha256_hash="2" * 64))
    names = [m.display_name for m in await repo.list_all()]
    assert names == ["Alpha", "Zeta"]


async def test_list_all_filters_by_loader_type() -> None:
    repo = FakeModRepository()
    await repo.add(_make_mod(loader_type="fabric", sha256_hash="1" * 64))
    await repo.add(_make_mod(loader_type="forge", sha256_hash="2" * 64))
    rows = await repo.list_all(loader_type="forge")
    assert [m.loader_type for m in rows] == ["forge"]


async def test_list_all_filters_by_side() -> None:
    repo = FakeModRepository()
    await repo.add(_make_mod(side="server", sha256_hash="1" * 64))
    await repo.add(_make_mod(side="client", sha256_hash="2" * 64))
    rows = await repo.list_all(side="client")
    assert [m.side for m in rows] == ["client"]


async def test_list_all_filters_by_mc_version() -> None:
    repo = FakeModRepository()
    await repo.add(_make_mod(mc_versions=["1.20.4"], sha256_hash="1" * 64))
    await repo.add(_make_mod(mc_versions=["1.21", "1.21.1"], sha256_hash="2" * 64))
    rows = await repo.list_all(mc_version="1.21")
    assert [m.mc_versions for m in rows] == [["1.21", "1.21.1"]]


async def test_delete_removes_row() -> None:
    repo = FakeModRepository()
    mod = _make_mod()
    await repo.add(mod)
    await repo.delete(mod.id)
    assert await repo.get_by_id(mod.id) is None


def _make_assignment(
    server_id: ServerId, mod_id: ModId, *, enabled: bool = True
) -> ServerModAssignment:
    now = dt.datetime.now(dt.timezone.utc)
    return ServerModAssignment(
        id=ServerModId.new(),
        server_id=server_id,
        mod_id=mod_id,
        enabled=enabled,
        assigned_by=uuid.uuid4(),
        created_at=now,
        updated_at=now,
    )


async def test_add_then_get_assignment_round_trips() -> None:
    repo = FakeModRepository()
    server_id = ServerId(uuid.uuid4())
    mod_id = ModId.new()
    assignment = _make_assignment(server_id, mod_id)
    await repo.add_assignment(assignment)
    assert await repo.get_assignment(server_id, mod_id) is assignment


async def test_get_assignment_absent_returns_none() -> None:
    repo = FakeModRepository()
    assert await repo.get_assignment(ServerId(uuid.uuid4()), ModId.new()) is None


async def test_list_assignments_for_server() -> None:
    repo = FakeModRepository()
    server_id = ServerId(uuid.uuid4())
    other = ServerId(uuid.uuid4())
    await repo.add_assignment(_make_assignment(server_id, ModId.new()))
    await repo.add_assignment(_make_assignment(server_id, ModId.new()))
    await repo.add_assignment(_make_assignment(other, ModId.new()))
    rows = await repo.list_assignments_for_server(server_id)
    assert len(rows) == 2
    assert all(r.server_id == server_id for r in rows)


async def test_list_assignments_for_mod() -> None:
    repo = FakeModRepository()
    mod_id = ModId.new()
    await repo.add_assignment(_make_assignment(ServerId(uuid.uuid4()), mod_id))
    await repo.add_assignment(_make_assignment(ServerId(uuid.uuid4()), mod_id))
    await repo.add_assignment(_make_assignment(ServerId(uuid.uuid4()), ModId.new()))
    rows = await repo.list_assignments_for_mod(mod_id)
    assert len(rows) == 2
    assert all(r.mod_id == mod_id for r in rows)


async def test_set_assignment_enabled_persists_flag() -> None:
    repo = FakeModRepository()
    server_id = ServerId(uuid.uuid4())
    mod_id = ModId.new()
    assignment = _make_assignment(server_id, mod_id, enabled=True)
    await repo.add_assignment(assignment)
    assignment.enabled = False
    await repo.set_assignment_enabled(assignment)
    stored = await repo.get_assignment(server_id, mod_id)
    assert stored is not None
    assert stored.enabled is False


async def test_delete_assignment_removes_row() -> None:
    repo = FakeModRepository()
    server_id = ServerId(uuid.uuid4())
    mod_id = ModId.new()
    await repo.add_assignment(_make_assignment(server_id, mod_id))
    await repo.delete_assignment(server_id, mod_id)
    assert await repo.get_assignment(server_id, mod_id) is None
