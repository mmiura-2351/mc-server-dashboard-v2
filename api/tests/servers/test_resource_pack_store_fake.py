"""Fidelity of :class:`FakeResourcePackStore` against the real adapter (#2330).

The double stands in for :class:`ObjectResourcePackStore` in every use-case and
route test, so a route reading a pack that is not stored must meet the *same*
failure from both. The adapter raises :class:`ResourcePackNotFoundError` — from
``head_object`` in ``size()`` and from ``get_object`` while the body streams in
``open()`` (see ``test_resource_pack_store_adapter.py``). These assertions
mirror that file's unknown-pack cases so the fake cannot drift back.
"""

from __future__ import annotations

import uuid

import pytest

from mc_server_dashboard_api.servers.domain.errors import ResourcePackNotFoundError
from mc_server_dashboard_api.servers.domain.resource_pack import ResourcePackId
from tests.servers.fakes import FakeResourcePackStore

_FILENAME = "pack.zip"


async def test_size_of_unknown_pack_raises_not_found() -> None:
    store = FakeResourcePackStore()

    with pytest.raises(ResourcePackNotFoundError):
        await store.size(ResourcePackId(uuid.uuid4()), _FILENAME)


async def test_open_of_unknown_pack_raises_not_found_while_streaming() -> None:
    store = FakeResourcePackStore()

    # The adapter's open() performs no I/O itself: it hands back a generator and
    # the miss surfaces on the first chunk. The fake must fail at the same point.
    stream = store.open(ResourcePackId(uuid.uuid4()), _FILENAME)

    with pytest.raises(ResourcePackNotFoundError):
        assert [chunk async for chunk in stream]
