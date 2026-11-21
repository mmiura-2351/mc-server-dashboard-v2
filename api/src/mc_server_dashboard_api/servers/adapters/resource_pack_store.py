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

- An interrupted :meth:`put` leaves no pack row: the row is inserted only after
  ``put`` returns. It usually leaves no blob either — the object client aborts
  the multipart upload before it reports the outage, and an incomplete upload is
  never an object — but not always: when the store completes the upload and the
  response is lost, the abort finds nothing to undo and the completed blob
  stays, referenced by no row and therefore never served, until the
  resource-pack GC reclaims it (issue #3277). Every upload mints a fresh pack id,
  so a retry shares no key with the failed attempt either way: the upload route
  answers 503 ``storage_unavailable``.
- An interrupted :meth:`delete` may have removed some of the pack's objects. It
  is idempotent — it re-lists the prefix, and the store treats deleting an absent
  key as success — so re-running it finishes the job. Its one caller commits the
  row's delete first and treats this as best-effort cleanup, so the outage is
  logged there rather than answered: the pack is already gone, and a 503 would
  invite a retry that can only find a 404. The same GC reclaims what it leaves.

:meth:`list_entries` feeds that GC: every blob under the namespace, with its key,
the pack id the key names, its size and its store time.

An error names its key repr'd (issue #3922): the key ends in the filename, a pack
stored before the upload refused control characters may carry them, and a
traceback that logs the error keeps its line breaks.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing

from mc_server_dashboard_api.servers.domain.errors import (
    ResourcePackNotFoundError,
    ResourcePackStorageUnavailableError,
    ResourcePackStorageUnreachableError,
)
from mc_server_dashboard_api.servers.domain.resource_pack import ResourcePackId
from mc_server_dashboard_api.servers.domain.resource_pack_store import (
    ResourcePackStore,
    StoredBlob,
)
from mc_server_dashboard_api.storage.adapters.object_store import (
    KeyNamespace,
    S3ClientFactory,
)
from mc_server_dashboard_api.storage.domain.errors import (
    NotFoundError,
    ObjectStoreUnavailableError,
    ObjectStoreUnreachableError,
)


def _prefix(pack_id: ResourcePackId) -> str:
    return f"{KeyNamespace.RESOURCE_PACKS}{pack_id.value}/"


def _key(pack_id: ResourcePackId, filename: str) -> str:
    return _prefix(pack_id) + filename


def _pack_id_of(key: str) -> ResourcePackId | None:
    """The pack a ``resource-packs/<pack-id>/<filename>`` key names, else ``None``.

    Only a key :func:`_key` could have built names a pack: a canonical UUID and a
    non-empty filename. A stray key under the namespace is no pack's, so it is
    neither an error (one would stop every GC pass for good) nor a pack id the GC
    could act on (issue #3277).
    """

    head, _, filename = key.removeprefix(KeyNamespace.RESOURCE_PACKS).partition("/")
    try:
        pack_uuid = uuid.UUID(head)
    except ValueError:
        return None
    if not filename or str(pack_uuid) != head:
        return None
    return ResourcePackId(pack_uuid)


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
            # The client has already tried to abort the multipart upload. A blob
            # can still be left under this key -- a completion whose response was
            # lost cannot be aborted -- but no row will name it (issues #2458,
            # #3277).
            raise ResourcePackStorageUnavailableError(repr(key)) from exc

    def open(self, pack_id: ResourcePackId, filename: str) -> AsyncIterator[bytes]:
        return self._open_gen(pack_id, filename)

    async def _open_gen(
        self, pack_id: ResourcePackId, filename: str
    ) -> AsyncIterator[bytes]:
        key = _key(pack_id, filename)
        try:
            async with self._client_factory() as client:
                async with aclosing(await client.get_object(key)) as body:
                    async for chunk in body:
                        yield chunk
        except NotFoundError as exc:
            # ``open`` does no I/O itself, so this fires on the first iteration.
            # The download routes begin the stream before writing the headers
            # (issue #2455), so that lands as their 404 rather than as a body
            # missing from an already-committed 200.
            raise ResourcePackNotFoundError(repr(key)) from exc
        except ObjectStoreUnavailableError as exc:
            # A store outage on the locating half of the read reaches the route
            # before any byte is on the wire (issue #2455), so it must arrive as
            # the servers type the routes answer 503 for rather than as a storage
            # type crossing the seam. An outage that strikes mid-body translates
            # the same way and still aborts the response — the status is committed
            # by then (issue #2337).
            raise ResourcePackStorageUnavailableError(repr(key)) from exc

    async def delete(self, pack_id: ResourcePackId) -> None:
        prefix = _prefix(pack_id)
        try:
            async with self._client_factory() as client:
                objects = await client.list_objects(prefix)
                for obj in objects:
                    await client.delete_object(obj.key)
        except ObjectStoreUnreachableError as exc:
            # Kept apart from the wider type: the GC ends its pass on this one
            # (issue #3871).
            raise ResourcePackStorageUnreachableError(prefix) from exc
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
            raise ResourcePackStorageUnavailableError(repr(key)) from exc
        if result is None:
            raise ResourcePackNotFoundError(repr(key))
        return result

    async def list_entries(self) -> list[StoredBlob]:
        prefix = KeyNamespace.RESOURCE_PACKS
        try:
            async with self._client_factory() as client:
                objects = await client.list_objects(prefix)
        except ObjectStoreUnavailableError as exc:
            raise ResourcePackStorageUnavailableError(prefix) from exc
        return [
            StoredBlob(
                pack_id=pack_id,
                key=obj.key,
                size_bytes=obj.size,
                modified_at=obj.last_modified,
            )
            for obj in objects
            if (pack_id := _pack_id_of(obj.key)) is not None
        ]
