"""The hydrate route over an object store whose bodies die mid-transfer (#2380).

The object client reports any mid-body read failure as ``ObjectStoreUnavailableError``
(``_iter_body``, issue #2375). The data plane maps no storage outage to a status
of its own, so before the headers are written that type reaches the edge as the
generic 500 — which the Worker treats like every other non-200/204 answer: the
hydrate fails and the start is retried. After the headers it aborts the body.

What these tests guard is that the failure is never answered as something it is
not: never the 204 that tells the Worker nothing is published (it would launch
on an empty or stale directory), never a tar missing the resolved JAR, never a
body that ends cleanly short of the working set — and that the transfer slot is
handed back either way, so one store blip cannot starve every later hydrate.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.dependencies import (
    get_resolved_jar_lookup,
    get_storage,
    get_transfer_semaphore,
    get_worker_credential,
)
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.errors import ObjectStoreUnavailableError
from mc_server_dashboard_api.storage.domain.value_objects import CommunityId, ServerId
from tests.storage.fake_s3 import FakeS3Store, close_tracking_factory, fake_s3_factory
from tests.storage.helpers import tar_stream

_CREDENTIAL = "test-worker-credential"
_CONTENT = bytes(range(256)) * 16
_JAR = b"PK\x03\x04 resolved server jar"

_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


def _setup(
    *, resolved_jar: str | None = None, raise_server_exceptions: bool = False
) -> tuple[TestClient, ObjectStorage, FakeS3Store, asyncio.Semaphore]:
    app = _shared_app
    app.dependency_overrides.clear()
    backing = FakeS3Store()
    storage = ObjectStorage(close_tracking_factory(fake_s3_factory(backing)))
    app.dependency_overrides[get_storage] = lambda: storage

    async def _lookup(_c: uuid.UUID, _s: uuid.UUID) -> str | None:
        return resolved_jar

    app.dependency_overrides[get_resolved_jar_lookup] = lambda: _lookup
    app.dependency_overrides[get_worker_credential] = lambda: _CREDENTIAL
    semaphore = asyncio.Semaphore(1)
    app.dependency_overrides[get_transfer_semaphore] = lambda: semaphore
    # A failure before the headers is rendered by the catch-all 500 handler, which
    # Starlette then re-raises; by default the client sees the response it rendered.
    client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
    return client, storage, backing, semaphore


def _hydrate_url(community: uuid.UUID, server: uuid.UUID) -> str:
    return f"/api/data-plane/communities/{community}/servers/{server}/working-set"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_CREDENTIAL}"}


async def _publish(
    storage: ObjectStorage, community: uuid.UUID, server: uuid.UUID
) -> None:
    handle = await storage.begin_snapshot(CommunityId(community), ServerId(server))
    await storage.write_snapshot(
        handle, tar_stream({"server.properties": b"motd=hi", "level.dat": _CONTENT})
    )
    await storage.commit_snapshot(handle)


async def _store_jar(storage: ObjectStorage) -> str:
    async def _stream() -> AsyncIterator[bytes]:
        yield _JAR

    return (await storage.put_jar(_stream())).sha256


def test_pointer_read_failure_is_500_not_the_unpublished_204() -> None:
    """The first read of a hydrate resolves the live snapshot from the pointer
    object. A body that dies there is not the missing pointer of an unpublished
    server, so it must not be answered with the 204 that means "nothing
    published"."""

    client, storage, backing, semaphore = _setup()
    community, server = uuid.uuid4(), uuid.uuid4()
    asyncio.run(_publish(storage, community, server))
    pointer = f"communities/{community}/servers/{server}/current.json"
    backing.read_aborts[pointer] = [1]

    with client:
        resp = client.get(_hydrate_url(community, server), headers=_auth())

    assert resp.status_code == 500
    assert resp.json()["reason"] == "internal_error"
    assert not semaphore.locked()


def test_jar_read_failure_is_500_not_a_tar_without_the_jar() -> None:
    """The resolved JAR is read whole before the headers. A body that dies
    partway is not the absent-from-pool JAR the route sends the working set
    alone for: the Worker must not launch against a stale embedded jar."""

    sha256 = hashlib.sha256(_JAR).hexdigest()
    client, storage, backing, semaphore = _setup(resolved_jar=sha256)
    community, server = uuid.uuid4(), uuid.uuid4()
    assert asyncio.run(_store_jar(storage)) == sha256
    asyncio.run(_publish(storage, community, server))
    backing.read_aborts[f"jars/{sha256}.jar"] = [4]

    with client:
        resp = client.get(_hydrate_url(community, server), headers=_auth())

    assert resp.status_code == 500
    assert resp.json()["reason"] == "internal_error"
    assert not semaphore.locked()


def test_member_failure_after_the_headers_aborts_the_body() -> None:
    """Once the 200 and the generation header are on the wire there is no status
    left to choose: the body must abort rather than finish a well-formed tar
    that is silently missing the rest of the working set."""

    # No response is rendered after the headers: surface the exception itself.
    client, storage, backing, semaphore = _setup(raise_server_exceptions=True)
    community, server = uuid.uuid4(), uuid.uuid4()
    asyncio.run(_publish(storage, community, server))
    member = next(
        key
        for key in backing.objects
        if "/snapshots/" in key and key.endswith("/level.dat")
    )
    backing.read_aborts[member] = [1000]

    with client, pytest.raises(ObjectStoreUnavailableError):
        client.get(_hydrate_url(community, server), headers=_auth())

    assert not semaphore.locked()
