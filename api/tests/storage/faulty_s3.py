"""An S3 stub whose calls can be answered with a store outage (issue #3233).

Wraps :func:`fake_s3_factory` so a test can pick, per call, where the store stops
answering — by operation name and by the key the call addresses — and so drive
the production adapters through an outage at an exact step of a multi-call
operation. The injected failure is the type the real object client raises for a
backend 5xx or a transport failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from mc_server_dashboard_api.storage.adapters.object_store import (
    S3Client,
    S3ClientFactory,
)
from mc_server_dashboard_api.storage.domain.errors import ObjectStoreUnavailableError
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory

# (operation name, the key it addresses) -> does the store fail this call?
Fault = Callable[[str, str], bool]


class Faults:
    """The outage switch: a predicate over each S3 call the adapter makes."""

    def __init__(self) -> None:
        self.when: Fault = lambda op, key: False

    def always(self) -> None:
        self.when = lambda op, key: True

    def clear(self) -> None:
        self.when = lambda op, key: False


class _FaultyClient:
    """An S3 client that answers a chosen call with the store's outage type."""

    def __init__(self, inner: S3Client, faults: Faults) -> None:
        self._inner = inner
        self._faults = faults

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        method = getattr(self._inner, name)

        async def _call(*args: Any, **kwargs: Any) -> Any:
            if self._faults.when(name, str(args[0]) if args else ""):
                raise ObjectStoreUnavailableError(f"injected outage: {name}")
            return await method(*args, **kwargs)

        return _call


def faulty_s3_factory(backing: FakeS3Store, faults: Faults) -> S3ClientFactory:
    inner = fake_s3_factory(backing)

    @asynccontextmanager
    async def _factory() -> AsyncIterator[S3Client]:
        async with inner() as client:
            yield _FaultyClient(client, faults)  # type: ignore[misc]

    return _factory
