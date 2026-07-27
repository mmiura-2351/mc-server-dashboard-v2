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
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.servers.domain.errors import ResourcePackNotFoundError
from mc_server_dashboard_api.servers.domain.resource_pack import ResourcePackId
from tests.servers.fakes import FakeResourcePackStore

_FILENAME = "pack.zip"


async def _stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


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


async def test_size_of_unknown_filename_is_not_found_like_open() -> None:
    # The adapter keys on ``resource-packs/<pack-id>/<filename>``, so a stored
    # pack read under a different filename misses (issue #2335). A fake keyed on
    # the pack id alone would serve the blob and hide a production 404.
    store = FakeResourcePackStore()
    pack_id = ResourcePackId(uuid.uuid4())
    await store.put(pack_id, _FILENAME, _stream(b"pack-bytes"))

    with pytest.raises(ResourcePackNotFoundError):
        assert [chunk async for chunk in store.open(pack_id, "other.zip")]
    with pytest.raises(ResourcePackNotFoundError):
        await store.size(pack_id, "other.zip")
