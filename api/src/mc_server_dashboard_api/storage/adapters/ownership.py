"""The store owner marker (STORAGE.md Section 4.3, issue #3870).

The JAR-pool, plugin-cache and resource-pack GCs delete any stored object no row
of this API's database names, and the storage sweep deletes staging and multipart
uploads no lease in this process holds. Pointed at a store that another database
also uses, they delete that database's live objects. A store (an object-store
bucket, or an fs root) must therefore belong to exactly one database, and this
module records which one.

At startup the API **claims** its store with its database's installation id
(DATABASE.md Section 1.1): it keeps a marker that exists, and writes one naming
that id when none does, but only on evidence that the store is this database's
(issue #3886). An empty store is. A store that holds objects is if the
communities and resource packs its keys name, sampled a few per namespace, are
rows of this database; one that holds only content-addressed objects (JARs,
plugin blobs) is if the database has rows that name stored objects at all.
Otherwise the store may be another database's: the API leaves it unclaimed, and
the operator claims it with ``store_claim_cli``. The write is conditional
(``If-None-Match: *`` on the object store, a hard link on fs), so of two APIs
starting together on an unmarked store exactly one takes it. A store that rejects
conditional writes gets a plain write.

Before every destructive pass the marker is read again, and :meth:`refusal` says
why the pass must not run unless the marker names this installation. A missing,
unreadable or foreign marker refuses; so does a store this process never claimed.
A process that did not claim its store at startup claims it only at its next
start, even once a marker names its installation. One that did claim it runs its
passes whenever the marker names its installation, so after a marker is removed
they resume once it is back; an operator handing the store to another database
stops this one first. Re-reading on every pass also catches the race a plain
write leaves open.
"""

from __future__ import annotations

import abc
import asyncio
import datetime as dt
import itertools
import json
import logging
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mc_server_dashboard_api.rfc3339 import serialize_utc
from mc_server_dashboard_api.storage.adapters.fs import _is_stale_spool
from mc_server_dashboard_api.storage.adapters.object_store import (
    ConditionalPutUnsupportedError,
    KeyNamespace,
    S3Client,
    S3ClientFactory,
)
from mc_server_dashboard_api.storage.domain.errors import NotFoundError

# The marker's key in the bucket and its name at the fs root. It sits outside
# every ``KeyNamespace`` prefix and outside ``communities/`` and ``jars/`` on fs,
# so no sweep or GC lists it.
OWNER_MARKER = "mcsd-owner.json"

# How many communities, and how many resource packs, the startup claim samples
# from a store without a marker (issue #3886).
_SAMPLE = 3

# How the startup claim reads each namespace (issue #3886): by the id its keys
# start with, or as content-addressed objects that name no row. A namespace in
# neither holds objects the claim cannot match against this database, so one
# that holds an object refuses the claim; a namespace added to ``KeyNamespace``
# is classified here before a store holding it is claimed.
_ID_NAMESPACES = (KeyNamespace.COMMUNITIES, KeyNamespace.RESOURCE_PACKS)
_CONTENT_NAMESPACES = (KeyNamespace.JARS, KeyNamespace.PLUGIN_CACHE)

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


def _already_marked(
    location: str, body: bytes | None, installation_id: uuid.UUID
) -> str:
    """Why the operator's claim did not claim the store, from the marker it read
    back."""

    if body is None:
        return (
            f"the store has no owner marker ({location}) after the claim: one was "
            "removed while the command ran. The store was not claimed; run the "
            "command again"
        )
    owner = _named_installation(body)
    if owner == installation_id:
        return (
            f"the store is already this database's: its owner marker {location} "
            f"names this installation, {installation_id}. Nothing was changed; "
            "restart the API if its passes still refuse"
        )
    if owner is None:
        return (
            f"the store has an owner marker ({location}) that names no "
            "installation. The store was not claimed: the claim command never "
            "replaces a marker (STORAGE.md Section 4.3)"
        )
    return (
        f"the store owner marker {location} names installation {owner}, not this "
        f"database's {installation_id}: the store is another database's. It was "
        "not claimed: the claim command never replaces a marker (STORAGE.md "
        "Section 4.3)"
    )


class StoreDatabase(Protocol):
    """What the startup claim asks this database about a store without a marker."""

    async def names_stored_objects(self) -> bool:
        """Whether any row of this database names a stored object."""
        ...

    async def has(
        self,
        *,
        communities: frozenset[uuid.UUID],
        resource_packs: frozenset[uuid.UUID],
    ) -> bool:
        """Whether every one of these communities and resource packs is a row."""
        ...


@dataclass(frozen=True)
class _Sample:
    """What a store without a marker holds, from a few keys per namespace.

    ``communities`` and ``resource_packs`` are the distinct ids the first keys of
    their namespaces start with, as found; ``content`` is whether a
    content-addressed namespace (JARs, plugin blobs) holds an object, asked only
    when no id was found; ``unclassified`` names the namespaces outside both kinds
    that hold an object.
    """

    communities: tuple[str, ...] = ()
    resource_packs: tuple[str, ...] = ()
    content: bool = False
    unclassified: tuple[str, ...] = ()


def _uuids(names: Iterable[str]) -> frozenset[uuid.UUID] | None:
    """The ids ``names`` spell, or ``None`` if any is not an id."""

    try:
        return frozenset(uuid.UUID(name) for name in names)
    except ValueError:
        return None


async def _not_ours(sample: _Sample, database: StoreDatabase) -> str | None:
    """Why a store without a marker must not be claimed, or ``None`` if it may."""

    if sample.unclassified:
        return (
            f"holds objects under {list(sample.unclassified)}, a namespace the claim "
            "cannot match against this database"
        )
    if sample.communities or sample.resource_packs:
        # Keys that name a community or a resource pack: the store is this
        # database's only if every one sampled is a row here.
        communities = _uuids(sample.communities)
        resource_packs = _uuids(sample.resource_packs)
        if (
            communities is None
            or resource_packs is None
            or not await database.has(
                communities=communities, resource_packs=resource_packs
            )
        ):
            return (
                f"holds objects of communities {list(sample.communities)} and "
                f"resource packs {list(sample.resource_packs)}, not all of which "
                "are rows of this database"
            )
        return None
    if sample.content and not await database.names_stored_objects():
        # Content-addressed objects name no row to match, so fall back to whether
        # this database is a fresh one.
        return (
            "holds JARs or plugin blobs, but no row of this database names a "
            "stored object"
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
        self, installation_id: uuid.UUID, *, database: StoreDatabase
    ) -> None:
        """Claim the store for ``installation_id`` at startup.

        A store without a marker is claimed only on evidence that it is this
        database's (:func:`_not_ours`). The store is sampled, and ``database``
        asked, only then, never on a pass. Otherwise the store stays unclaimed and
        every pass of this process refuses.
        """

        created = False
        if await self._read() is None:
            reason = await _not_ours(await self._sample(), database)
            if reason is not None:
                _LOG.error(
                    "the store has no owner marker (%s) and %s (installation %s), "
                    "so it may be another database's. This API did not claim it, "
                    "and GCs and destructive storage sweeps will not run. If the "
                    "store belongs to this database, run `%s` and restart the API "
                    "(STORAGE.md Section 4.3, issue #3886)",
                    self._location,
                    reason,
                    installation_id,
                    CLAIM_COMMAND,
                )
                return
            created = await self._create(installation_id)
        self._installation_id = installation_id
        _log_claim(self._location, created, await self.refusal())

    async def claim_explicitly(self, installation_id: uuid.UUID) -> str | None:
        """Write the marker for ``installation_id`` whatever the store holds.

        The operator's claim (``store_claim_cli``). It samples nothing and deletes
        nothing, and it never replaces a marker that exists when it reads one. It
        reads the marker back after writing, so a store without conditional writes
        that took another claimant's marker last is reported. It returns ``None``
        once the marker it wrote names this installation, and otherwise why the
        store was not claimed.
        """

        created = await self._read() is None and await self._create(installation_id)
        body = await self._read()
        if (
            created
            and body is not None
            and _named_installation(body) == installation_id
        ):
            _LOG.info(
                "claimed the store for installation %s: wrote %s",
                installation_id,
                self._location,
            )
            return None
        return _already_marked(self._location, body, installation_id)

    async def refusal(self) -> str | None:
        """Why a destructive pass must not run now, or ``None`` if the store is ours."""

        return _refusal(self._location, await self._read(), self._installation_id)

    async def refusal_for(self, installation_id: uuid.UUID) -> str | None:
        """Why an operator command's destructive pass must not run, or ``None``.

        For a command that never claims the store (issue #4107): the pass may
        run only while the marker names ``installation_id``, the installation of
        the database the command was pointed at. It reads the marker and writes
        nothing.
        """

        return _refusal(self._location, await self._read(), installation_id)

    @abc.abstractmethod
    async def _read(self) -> bytes | None:
        """The marker's bytes, or ``None`` if there is none."""

    @abc.abstractmethod
    async def _sample(self) -> _Sample:
        """A few keys of each namespace a GC or sweep lists."""

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

    async def _sample(self) -> _Sample:
        # A few listing requests per namespace, so the sample of a large bucket
        # stays cheap.
        classified = (*_ID_NAMESPACES, *_CONTENT_NAMESPACES)
        async with self._client_factory() as client:
            communities = await _first_ids(client, KeyNamespace.COMMUNITIES)
            resource_packs = await _first_ids(client, KeyNamespace.RESOURCE_PACKS)
            unclassified = tuple(
                [
                    namespace.value
                    for namespace in KeyNamespace
                    if namespace not in classified
                    and await client.first_key(namespace) is not None
                ]
            )
            # Ids decide when there are any, so the content namespaces are listed
            # only without them.
            content = False
            if not (communities or resource_packs):
                for namespace in _CONTENT_NAMESPACES:
                    if await client.first_key(namespace) is not None:
                        content = True
                        break
        return _Sample(communities, resource_packs, content, unclassified)

    async def _create(self, installation_id: uuid.UUID) -> bool:
        body = _marker_body(installation_id)
        async with self._client_factory() as client:
            try:
                return await client.put_object_if_absent(OWNER_MARKER, body)
            except ConditionalPutUnsupportedError:
                await client.put_object(OWNER_MARKER, body)
                return True


async def _first_ids(client: S3Client, namespace: str) -> tuple[str, ...]:
    """Up to :data:`_SAMPLE` distinct ids that keys under ``namespace`` start
    with, lowest first: ``communities/<id>/...``, ``resource-packs/<id>/...``.

    One listing per id. Each starts after every key of the id before it: a key of
    that id continues with ``/``, which sorts just below ``0``. It lists keys, not
    common prefixes (``Delimiter=/``): SeaweedFS reports a directory left empty by
    deletes as a common prefix, so a prefix listing would count ids that hold no
    object.
    """

    ids: list[str] = []
    start_after = None
    while len(ids) < _SAMPLE:
        key = await client.first_key(namespace, start_after=start_after)
        if key is None:
            break
        name = key[len(namespace) :].split("/", 1)[0]
        ids.append(name)
        start_after = f"{namespace}{name}0"
    return tuple(ids)


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
        self, installation_id: uuid.UUID, *, database: StoreDatabase
    ) -> None:
        await asyncio.to_thread(self._remove_stale_temps)
        await super().claim(installation_id, database=database)

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

    async def _sample(self) -> _Sample:
        # FsStorage keeps only ``communities/<id>/`` and ``jars/`` at the root; no
        # resource packs are stored on fs. Ids decide when there are any, so
        # ``jars/`` is read only without them.
        communities = await asyncio.to_thread(self._entries, "communities")
        content = not communities and bool(
            await asyncio.to_thread(self._entries, "jars")
        )
        return _Sample(communities=communities, content=content)

    def _entries(self, name: str) -> tuple[str, ...]:
        # Up to :data:`_SAMPLE` entries of one namespace directory, so the sample
        # of a large root stays cheap. A missing root or directory holds nothing;
        # a directory emptied by deletes still counts, which errs safe.
        try:
            with os.scandir(self._root / name) as entries:
                return tuple(entry.name for entry in itertools.islice(entries, _SAMPLE))
        except FileNotFoundError:
            return ()

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
