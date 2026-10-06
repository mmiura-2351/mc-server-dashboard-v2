"""Object-store implementation of the ``ResourcePackStore`` Port.

Stores resource pack blobs under the ``resource-packs/<pack-id>/<filename>``
key namespace (top-level, outside ``communities/``). Uses the same
:class:`~...storage.adapters.object_store.S3ClientFactory` as the main
``ObjectStorage`` adapter.

The seam translates the storage error so no storage type crosses back into the
servers layer (mirroring ``backup_store.py``): a missing blob surfaces as
:class:`ResourcePackNotFoundError`, which the download routes map to 404 —
an orphaned row is not retrievable, not a degraded backend (issue #2321).

An outage is the store's other answer, and on the two calls a download makes —
:meth:`size` and :meth:`open` — it surfaces as
:class:`ResourcePackStorageUnavailableError`, which the edge maps to 503
``storage_unavailable`` (issue #2455). :meth:`open` included: the routes now begin
the stream before they write the headers, so the locating half of the read can
still choose that status, and one outage yields one status whichever of the two
calls it strikes. Only an outage that strikes once the body is already flowing has
no status left to choose, and stays the truncated body guarded by the routes' byte
count (issue #2337).

That holds only because the layer below produces the typed error in the first
place: the object client translates a backend 5xx / transport failure on
``head_object`` and ``get_object``, not just on the writes (issues #2376, #2378).

The write paths — :meth:`put` and :meth:`delete` — translate the same way (issue
#2458), and what their callers do with it follows from what a failure leaves
behind:

- An interrupted :meth:`put` leaves no blob. The object client aborts the
  multipart upload before it reports the outage, an incomplete upload is never an
  object, and the pack row is inserted only after ``put`` returns. Every upload
  mints a fresh pack id, so a retry shares no key with the failed attempt: the
  upload route answers 503 ``storage_unavailable``.
- An interrupted :meth:`delete` may have removed some of the pack's objects. It
  is idempotent — it re-lists the prefix, and the store treats deleting an absent
  key as success — so re-running it finishes the job. Its one caller commits the
  row's delete first and treats this as best-effort cleanup, so the outage is
  logged there rather than answered: the pack is already gone, and a 503 would
  invite a retry that can only find a 404.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.errors import (
    ResourcePackNotFoundError,
    ResourcePackStorageUnavailableError,
)
from mc_server_dashboard_api.servers.domain.resource_pack import ResourcePackId
from mc_server_dashboard_api.servers.domain.resource_pack_store import (
    ResourcePackStore,
)
from mc_server_dashboard_api.storage.adapters.object_store import S3ClientFactory
from mc_server_dashboard_api.storage.domain.errors import (
    NotFoundError,
    ObjectStoreUnavailableError,
)


def _key(pack_id: ResourcePackId, filename: str) -> str:
    return f"resource-packs/{pack_id.value}/{filename}"


class ObjectResourcePackStore(ResourcePackStore):
    """:class:`ResourcePackStore` adapter over an S3-compatible object store."""

    def __init__(self, client_factory: S3ClientFactory) -> None:
        self._client_factory = client_factory

    async def put(
        self, pack_id: ResourcePackId, filename: str, stream: AsyncIterator[bytes]
    ) -> None:
        key = _key(pack_id, filename)
        try:
            async with self._client_factory() as client:
                await client.upload_multipart(key, stream)
        except ObjectStoreUnavailableError as exc:
            # The client has already aborted the multipart upload, so nothing was
            # published under this key (issue #2458).
            raise ResourcePackStorageUnavailableError(key) from exc

    def open(self, pack_id: ResourcePackId, filename: str) -> AsyncIterator[bytes]:
        return self._open_gen(pack_id, filename)

    async def _open_gen(
        self, pack_id: ResourcePackId, filename: str
    ) -> AsyncIterator[bytes]:
        key = _key(pack_id, filename)
        try:
            async with self._client_factory() as client:
                async for chunk in await client.get_object(key):
                    yield chunk
        except NotFoundError as exc:
            # ``open`` does no I/O itself, so this fires on the first iteration.
            # The download routes begin the stream before writing the headers
            # (issue #2455), so that lands as their 404 rather than as a body
            # missing from an already-committed 200.
            raise ResourcePackNotFoundError(key) from exc
        except ObjectStoreUnavailableError as exc:
            # A store outage on the locating half of the read reaches the route
            # before any byte is on the wire (issue #2455), so it must arrive as
            # the servers type the routes answer 503 for rather than as a storage
            # type crossing the seam. An outage that strikes mid-body translates
            # the same way and still aborts the response — the status is committed
            # by then (issue #2337).
            raise ResourcePackStorageUnavailableError(key) from exc

    async def delete(self, pack_id: ResourcePackId) -> None:
        prefix = f"resource-packs/{pack_id.value}/"
        try:
            async with self._client_factory() as client:
                objects = await client.list_objects(prefix)
                for obj in objects:
                    await client.delete_object(obj.key)
        except ObjectStoreUnavailableError as exc:
            # Possibly partway through the prefix. Re-running lists only what is
            # left, so the objects already removed are not an error (issue #2458).
            raise ResourcePackStorageUnavailableError(prefix) from exc

    async def size(self, pack_id: ResourcePackId, filename: str) -> int:
        key = _key(pack_id, filename)
        try:
            async with self._client_factory() as client:
                result = await client.head_object(key)
        except ObjectStoreUnavailableError as exc:
            # The probe the download routes declare Content-Length from. It sits in
            # the same window as the open above and answers the same route, so one
            # outage must not be a 503 or a 500 depending on which call it struck
            # (issue #2455).
            raise ResourcePackStorageUnavailableError(key) from exc
        if result is None:
            raise ResourcePackNotFoundError(key)
        return result
