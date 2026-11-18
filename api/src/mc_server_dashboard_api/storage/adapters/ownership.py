"""The store owner marker (STORAGE.md Section 4.3, issue #3870).

The JAR-pool, plugin-cache and resource-pack GCs delete any stored object no row
of this API's database names, and the storage sweep deletes staging and multipart
uploads no lease in this process holds. Pointed at a store that another database
also uses, they delete that database's live objects. A store (an object-store
bucket, or an fs root) must therefore belong to exactly one database, and this
module records which one.

At startup the API **claims** its store with its database's installation id
(DATABASE.md Section 2.1): it writes an owner marker naming that id when none
exists, and keeps one that does. The write is conditional (``If-None-Match: *``
on the object store, a hard link on fs), so of two APIs starting together on an
unmarked store exactly one takes it. A store that rejects conditional writes gets
a plain write.

Before every destructive pass the marker is read again, and :meth:`refusal` says
why the pass must not run unless the marker names this installation. A missing,
unreadable or foreign marker refuses; so does a store this process never claimed.
A marker removed while the API runs is not claimed again until the next start, so
an operator handing the store to another database stops this one first. Re-reading
on every pass also catches the race a plain write leaves open.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import uuid
from pathlib import Path

from mc_server_dashboard_api.rfc3339 import serialize_utc
from mc_server_dashboard_api.storage.adapters.object_store import (
    ConditionalPutUnsupportedError,
    S3Client,
    S3ClientFactory,
)
from mc_server_dashboard_api.storage.domain.errors import NotFoundError

# The marker's key in the bucket and its name at the fs root. It sits outside
# every ``KeyNamespace`` prefix and outside ``communities/`` and ``jars/`` on fs,
# so no sweep or GC lists it.
OWNER_MARKER = "mcsd-owner.json"

_LOG = logging.getLogger(__name__)


def _marker_body(installation_id: uuid.UUID) -> bytes:
    return json.dumps(
        {
            "installation_id": str(installation_id),
            "created_at": serialize_utc(dt.datetime.now(dt.UTC)),
        }
    ).encode()


def _named_installation(body: bytes) -> uuid.UUID | None:
    """The installation id the marker names, or ``None`` if it names none."""

    try:
        data = json.loads(body)
        return uuid.UUID(data["installation_id"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _refusal(
    location: str, body: bytes | None, installation_id: uuid.UUID | None
) -> str | None:
    if installation_id is None:
        return f"this API process has not claimed the store ({location})"
    if body is None:
        return (
            f"the store owner marker {location} is missing; restart the API to "
            "claim the store"
        )
    owner = _named_installation(body)
    if owner is None:
        return f"the store owner marker {location} names no installation"
    if owner != installation_id:
        return (
            f"the store owner marker {location} names installation {owner}, but "
            f"this database is installation {installation_id}: the store belongs "
            "to another database (STORAGE.md Section 4.3)"
        )
    return None


def _log_claim(location: str, created: bool, refusal: str | None) -> None:
    if refusal is not None:
        _LOG.error(
            "%s; GCs and destructive storage sweeps will not run (issue #3870)",
            refusal,
        )
    elif created:
        _LOG.info("claimed the store for this installation: wrote %s", location)


class ObjectStoreOwnership:
    """The owner marker of an object-store bucket, at key :data:`OWNER_MARKER`."""

    def __init__(self, client_factory: S3ClientFactory, *, bucket: str) -> None:
        self._client_factory = client_factory
        self._location = f"{OWNER_MARKER} in bucket {bucket!r}"
        self._installation_id: uuid.UUID | None = None

    async def claim(self, installation_id: uuid.UUID) -> None:
        """Write the marker for ``installation_id`` if none exists (at startup)."""

        created = False
        async with self._client_factory() as client:
            if await self._read(client) is None:
                body = _marker_body(installation_id)
                try:
                    created = await client.put_object_if_absent(OWNER_MARKER, body)
                except ConditionalPutUnsupportedError:
                    await client.put_object(OWNER_MARKER, body)
                    created = True
        self._installation_id = installation_id
        _log_claim(self._location, created, await self.refusal())

    async def refusal(self) -> str | None:
        """Why a destructive pass must not run now, or ``None`` if the store is ours."""

        async with self._client_factory() as client:
            body = await self._read(client)
        return _refusal(self._location, body, self._installation_id)

    @staticmethod
    async def _read(client: S3Client) -> bytes | None:
        try:
            chunks = await client.get_object(OWNER_MARKER)
        except NotFoundError:
            return None
        return b"".join([chunk async for chunk in chunks])


class FsStoreOwnership:
    """The owner marker of an fs storage root, at ``<root>/mcsd-owner.json``.

    The fs sweep is blocking and runs off the event loop, so it reads the marker
    through :meth:`refusal_blocking`.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._path = root / OWNER_MARKER
        self._installation_id: uuid.UUID | None = None

    async def claim(self, installation_id: uuid.UUID) -> None:
        """Write the marker for ``installation_id`` if none exists (at startup)."""

        created = await asyncio.to_thread(self._claim_blocking, installation_id)
        self._installation_id = installation_id
        _log_claim(str(self._path), created, await self.refusal())

    async def refusal(self) -> str | None:
        """Why a destructive pass must not run now, or ``None`` if the store is ours."""

        return await asyncio.to_thread(self.refusal_blocking)

    def refusal_blocking(self) -> str | None:
        """:meth:`refusal`, for a caller already off the event loop."""

        return _refusal(str(self._path), self._read_marker(), self._installation_id)

    def _read_marker(self) -> bytes | None:
        try:
            return self._path.read_bytes()
        except FileNotFoundError:
            return None

    def _claim_blocking(self, installation_id: uuid.UUID) -> bool:
        if self._read_marker() is not None:
            return False
        self._root.mkdir(parents=True, exist_ok=True)
        # Write the whole marker under a temporary name, then hard-link it into
        # place: the link fails if the marker exists, so of two claimants exactly
        # one creates it, and no reader ever sees it half written.
        tmp = self._root / f".{OWNER_MARKER}.{uuid.uuid4().hex}.tmp"
        try:
            with tmp.open("xb") as fh:
                fh.write(_marker_body(installation_id))
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, self._path)
            except FileExistsError:
                return False
        finally:
            tmp.unlink(missing_ok=True)
        fd = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
