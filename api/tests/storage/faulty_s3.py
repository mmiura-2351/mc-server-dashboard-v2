"""An S3 stub whose calls can be answered with a store outage (issue #3233).

Wraps :func:`fake_s3_factory` so a test can pick, per call, where the store stops
answering — by operation name and by the key the call addresses — and so drive
the production adapters through an outage at an exact step of a multi-call
operation. The injected failure is the type the real object client raises for a
backend 5xx or a transport failure.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
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
        # Clients currently inside their ``async with``: what a stream that was
        # opened and not yet closed is still holding. Counted down only once the
        # client's exit has run to completion (see :class:`_FaultyContext`).
        self.open_clients = 0
        # The ``get_object`` bodies (by key) that deliver their first chunk and
        # then never answer again: a read that is PENDING, for a test to
        # disconnect into. ``stalled`` is set once a body has reached that point.
        self.stall_body: Callable[[str], bool] = lambda key: False
        self.stalled = asyncio.Event()

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
            result = await method(*args, **kwargs)
            if name == "get_object" and self._faults.stall_body(str(args[0])):
                return _stalling(result, self._faults)
            return result

        return _call


async def _stalling(body: AsyncIterator[bytes], faults: Faults) -> AsyncIterator[bytes]:
    """Deliver ``body``'s first chunk, then leave the next read pending forever."""

    yield await anext(body)
    faults.stalled.set()
    await asyncio.Event().wait()


class _FaultyContext(AbstractAsyncContextManager[S3Client]):
    """One client's ``async with``, whose exit AWAITS like a real client's does.

    Releasing a real S3 client is asynchronous. The exit here yields to the event
    loop once before it counts the client as released, so cleanup that is
    cancelled partway — an exit running inside an already-cancelled scope — is
    visible as a client that never got counted down (issue #3234).
    """

    def __init__(
        self, inner: AbstractAsyncContextManager[S3Client], faults: Faults
    ) -> None:
        self._inner = inner
        self._faults = faults

    async def __aenter__(self) -> S3Client:
        client = await self._inner.__aenter__()
        self._faults.open_clients += 1
        return _FaultyClient(client, self._faults)  # type: ignore[return-value]

    async def __aexit__(self, *exc_info: Any) -> bool | None:
        await asyncio.sleep(0)
        suppress = await self._inner.__aexit__(*exc_info)
        self._faults.open_clients -= 1
        return suppress


def faulty_s3_factory(backing: FakeS3Store, faults: Faults) -> S3ClientFactory:
    inner = fake_s3_factory(backing)
    return lambda: _FaultyContext(inner(), faults)
