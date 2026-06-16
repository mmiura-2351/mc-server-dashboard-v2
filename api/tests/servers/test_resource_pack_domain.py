"""Domain entity unit tests for the resource pack module.

Tests the value objects and dataclasses: ``ResourcePackId``, ``ResourcePack``,
and ``ResourcePackAssignment``.
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackAssignment,
    ResourcePackId,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


def test_resource_pack_id_new_generates_unique_ids() -> None:
    a = ResourcePackId.new()
    b = ResourcePackId.new()
    assert a != b
    assert isinstance(a.value, uuid.UUID)


def test_resource_pack_id_wraps_uuid() -> None:
    raw = uuid.uuid4()
    pack_id = ResourcePackId(raw)
    assert pack_id.value is raw


def test_resource_pack_id_is_frozen() -> None:
    pack_id = ResourcePackId.new()
    try:
        pack_id.value = uuid.uuid4()  # type: ignore[misc]
        assert False, "should be frozen"
    except AttributeError:
        pass


def _make_pack(**overrides: object) -> ResourcePack:
    now = dt.datetime.now(dt.timezone.utc)
    defaults: dict[str, object] = {
        "id": ResourcePackId.new(),
        "filename": "my-pack.zip",
        "display_name": "My Resource Pack",
        "description": "A test pack",
        "sha1_hash": "a" * 40,
        "sha256_hash": "b" * 64,
        "size_bytes": 1024,
        "uploaded_by": uuid.uuid4(),
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return ResourcePack(**defaults)  # type: ignore[arg-type]


def test_resource_pack_fields() -> None:
    pack = _make_pack(filename="pack.zip", display_name="Pack")
    assert pack.filename == "pack.zip"
    assert pack.display_name == "Pack"


def test_resource_pack_description_nullable() -> None:
    pack = _make_pack(description=None)
    assert pack.description is None


def test_resource_pack_is_mutable() -> None:
    pack = _make_pack()
    new_name = "Updated Pack"
    pack.display_name = new_name
    assert pack.display_name == new_name


def _make_assignment(**overrides: object) -> ResourcePackAssignment:
    now = dt.datetime.now(dt.timezone.utc)
    defaults: dict[str, object] = {
        "server_id": ServerId.new(),
        "resource_pack_id": ResourcePackId.new(),
        "require_resource_pack": False,
        "resource_pack_prompt": None,
        "assigned_by": uuid.uuid4(),
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return ResourcePackAssignment(**defaults)  # type: ignore[arg-type]


def test_assignment_fields() -> None:
    sid = ServerId.new()
    pid = ResourcePackId.new()
    assignment = _make_assignment(
        server_id=sid,
        resource_pack_id=pid,
        require_resource_pack=True,
        resource_pack_prompt="Please install the pack",
    )
    assert assignment.server_id is sid
    assert assignment.resource_pack_id is pid
    assert assignment.require_resource_pack is True
    assert assignment.resource_pack_prompt == "Please install the pack"


def test_assignment_prompt_nullable() -> None:
    assignment = _make_assignment(resource_pack_prompt=None)
    assert assignment.resource_pack_prompt is None


def test_assignment_is_mutable() -> None:
    assignment = _make_assignment()
    assignment.require_resource_pack = True
    assert assignment.require_resource_pack is True
