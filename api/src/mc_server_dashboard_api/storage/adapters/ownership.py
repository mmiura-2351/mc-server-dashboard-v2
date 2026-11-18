"""The store owner marker (STORAGE.md Section 4.3, issue #3870).

The JAR-pool, plugin-cache and resource-pack GCs delete any stored object no row
of this API's database names, and the storage sweep deletes staging and multipart
uploads no lease in this process holds. Pointed at a store that another database
also uses, they delete that database's live objects. A store (an object-store
bucket, or an fs root) must therefore belong to exactly one database, and this
module records which one.

At startup the API **claims** its store with its database's installation id
(DATABASE.md Section 1.1): it keeps a marker that exists, and writes one naming
that id when none does, but only if the store holds no object yet or the database
has rows that name stored objects. A store that holds objects this database knows
nothing of may be another database's, so the API leaves it unclaimed and the
operator claims it with ``store_claim_cli`` (issue #3886). The write is
conditional (``If-None-Match: *`` on the object store, a hard link on fs), so of
two APIs starting together on an unmarked store exactly one takes it. A store
that rejects conditional writes gets a plain write.

Before every destructive pass the marker is read again, and :meth:`refusal` says
why the pass must not run unless the marker names this installation. A missing,
unreadable or foreign marker refuses; so does a store this process never claimed.
A marker removed or written while the API runs takes effect for it only at the
next start, so an operator handing the store to another database stops this one
first. Re-reading on every pass also catches the race a plain write leaves open.
"""

from __future__ import annotations

import abc
import asyncio
import datetime as dt
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from mc_server_dashboard_api.rfc3339 import serialize_utc
from mc_server_dashboard_api.storage.adapters.fs import _is_stale_spool
from mc_server_dashboard_api.storage.adapters.object_store import (
    ConditionalPutUnsupportedError,
    KeyNamespace,
    S3ClientFactory,
)
from mc_server_dashboard_api.storage.domain.errors import NotFoundError

# The marker's key in the bucket and its name at the fs root. It sits outside
# every ``KeyNamespace`` prefix and outside ``communities/`` and ``jars/`` on fs,
# so no sweep or GC lists it.
OWNER_MARKER = "mcsd-owner.json"

# The directories at the fs root a GC or sweep lists. The marker and a claim's
# temporary file sit beside them, at the root itself.
_FS_NAMESPACES = ("communities", "jars")

# What an operator runs to claim a store the API left unclaimed (issue #3886).
CLAIM_COMMAND = "python -m mc_server_dashboard_api.store_claim_cli"

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
        return (
            f"this API process has not claimed the store ({location}). If the "
            f"store belongs to this database, run `{CLAIM_COMMAND}` and restart "
            "the API (STORAGE.md Section 4.3)"
        )
    if body is None:
        return (
            f"the store owner marker {location} cannot be read back. If it was "
            "removed, only an API start or the claim command claims the store "
            "again; if it was just written, a later pass finds it"
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


class _StoreOwnership(abc.ABC):
    """The claim rules and the refusal, over one backend's marker and namespaces."""

    def __init__(self, location: str) -> None:
        self._location = location
        self._installation_id: uuid.UUID | None = None

    async def claim(
        self,
        installation_id: uuid.UUID,
        *,
        database_names_objects: Callable[[], Awaitable[bool]],
    ) -> None:
        """Claim the store for ``installation_id`` at startup.

        A store without a marker is claimed only if it holds no object or
        ``database_names_objects`` says this database has rows that name stored
        objects. Both probes run only then, never on a pass. Otherwise the store
        stays unclaimed and every pass of this process refuses.
        """

        created = False
        if await self._read() is None:
            if await self._holds_objects() and not await database_names_objects():
                _LOG.error(
                    "the store has no owner marker (%s) and holds objects, but no "
                    "row of this database (installation %s) names a stored object, "
                    "so the store may be another database's. This API did not claim "
                    "it, and GCs and destructive storage sweeps will not run. If the "
                    "store belongs to this database, run `%s` and restart the API "
                    "(STORAGE.md Section 4.3, issue #3886)",
                    self._location,
                    installation_id,
                    CLAIM_COMMAND,
                )
                return
            created = await self._create(installation_id)
        self._installation_id = installation_id
        _log_claim(self._location, created, await self.refusal())

    async def claim_explicitly(self, installation_id: uuid.UUID) -> str | None:
        """Write the marker for ``installation_id`` whatever the store holds.

        The operator's claim (``store_claim_cli``). It runs no probe and deletes
        nothing, and it never replaces a marker: it returns why it refused when one
        exists, naming this installation or another, and ``None`` once it wrote
        one.
        """

        if await self._read() is None and await self._create(installation_id):
            _LOG.info(
                "claimed the store for installation %s: wrote %s",
                installation_id,
                self._location,
            )
            return None
        body = await self._read()
        owner = None if body is None else _named_installation(body)
        return (
            f"the store already has an owner marker ({self._location}), naming "
            f"installation {owner}; this database is installation "
            f"{installation_id}. The claim never replaces a marker "
            "(STORAGE.md Section 4.3)"
        )

    async def refusal(self) -> str | None:
        """Why a destructive pass must not run now, or ``None`` if the store is ours."""

        return _refusal(self._location, await self._read(), self._installation_id)

    @abc.abstractmethod
    async def _read(self) -> bytes | None:
        """The marker's bytes, or ``None`` if there is none."""

    @abc.abstractmethod
    async def _holds_objects(self) -> bool:
        """Whether any namespace a GC or sweep lists holds an object."""

    @abc.abstractmethod
    async def _create(self, installation_id: uuid.UUID) -> bool:
        """Write the marker unless one exists; whether this call wrote it."""


class ObjectStoreOwnership(_StoreOwnership):
    """The owner marker of an object-store bucket, at key :data:`OWNER_MARKER`."""

    def __init__(self, client_factory: S3ClientFactory, *, bucket: str) -> None:
        super().__init__(f"{OWNER_MARKER} in bucket {bucket!r}")
        self._client_factory = client_factory

    async def _read(self) -> bytes | None:
        async with self._client_factory() as client:
            try:
                chunks = await client.get_object(OWNER_MARKER)
            except NotFoundError:
                return None
            return b"".join([chunk async for chunk in chunks])

    async def _holds_objects(self) -> bool:
        # One key per namespace, so the probe of a large bucket stays cheap.
        async with self._client_factory() as client:
            for namespace in KeyNamespace:
                if await client.any_object(namespace):
                    return True
        return False

    async def _create(self, installation_id: uuid.UUID) -> bool:
        body = _marker_body(installation_id)
        async with self._client_factory() as client:
            try:
                return await client.put_object_if_absent(OWNER_MARKER, body)
            except ConditionalPutUnsupportedError:
                await client.put_object(OWNER_MARKER, body)
                return True


class FsStoreOwnership(_StoreOwnership):
    """The owner marker of an fs storage root, at ``<root>/mcsd-owner.json``.

    The fs sweep is blocking and runs off the event loop, so it reads the marker
    through :meth:`refusal_blocking`.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(str(root / OWNER_MARKER))
        self._root = root
        self._path = root / OWNER_MARKER

    async def claim(
        self,
        installation_id: uuid.UUID,
        *,
        database_names_objects: Callable[[], Awaitable[bool]],
    ) -> None:
        await asyncio.to_thread(self._remove_stale_temps)
        await super().claim(
            installation_id, database_names_objects=database_names_objects
        )

    def refusal_blocking(self) -> str | None:
        """:meth:`refusal`, for a caller already off the event loop."""

        return _refusal(self._location, self._read_marker(), self._installation_id)

    async def _read(self) -> bytes | None:
        return await asyncio.to_thread(self._read_marker)

    def _read_marker(self) -> bytes | None:
        try:
            return self._path.read_bytes()
        except FileNotFoundError:
            return None

    async def _holds_objects(self) -> bool:
        return await asyncio.to_thread(self._holds_objects_blocking)

    def _holds_objects_blocking(self) -> bool:
        # One entry per namespace directory, so the probe of a large root stays
        # cheap. A missing root or directory holds nothing.
        for name in _FS_NAMESPACES:
            try:
                with os.scandir(self._root / name) as entries:
                    if next(entries, None) is not None:
                        return True
            except FileNotFoundError:
                continue
        return False

    def _remove_stale_temps(self) -> None:
        # A crash before the ``finally`` in ``_create_blocking`` leaves a temporary
        # file at the root. Remove one old enough that no claim still running can
        # own it.
        for stale in self._root.glob(f".{OWNER_MARKER}.*.tmp"):
            if _is_stale_spool(stale):
                stale.unlink(missing_ok=True)

    async def _create(self, installation_id: uuid.UUID) -> bool:
        return await asyncio.to_thread(self._create_blocking, installation_id)

    def _create_blocking(self, installation_id: uuid.UUID) -> bool:
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
