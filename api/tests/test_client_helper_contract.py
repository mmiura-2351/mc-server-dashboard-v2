"""Contract for the endpoint-test client helper (issue #1980).

``enter_client(TestClient(app))`` must keep the app's lifespan OPEN while the
test body runs its requests. The pattern it replaces -- ``next(_client(...))``
over a ``with TestClient(app): yield`` generator -- finalized the generator the
instant ``next()`` returned, running lifespan *shutdown* before the first
request. An instrumented lifespan therefore records ``["startup"]`` (not
``["startup", "shutdown"]``) at request time under the new helper.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.client_utils import enter_client


def _instrumented_app(events: list[str]) -> FastAPI:
    """An app whose lifespan appends to ``events`` on startup and shutdown."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        events.append("startup")
        yield
        events.append("shutdown")

    app = FastAPI(lifespan=lifespan)

    @app.get("/lifespan-events")
    async def _read() -> list[str]:
        return list(events)

    return app


def test_enter_client_keeps_lifespan_open_during_request() -> None:
    events: list[str] = []
    client = enter_client(TestClient(_instrumented_app(events)))
    resp = client.get("/lifespan-events")
    assert resp.status_code == 200
    # The lifespan started and has NOT yet shut down while the request runs.
    assert resp.json() == ["startup"]
    assert events == ["startup"]
