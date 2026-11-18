"""Stub validations held in flight together, to probe the default executor (#3843).

An upload door's archive validation is CPU-bound and can run for minutes. On
asyncio's default executor, as many of them as it has workers would leave nothing
for the rest of the process that runs there. A test installs a default executor
of :data:`IN_FLIGHT` workers, so that holds whatever the host's CPU count, holds
that many stub validations in flight with :class:`InFlight`, and checks that the
default executor still runs a call at once.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

# How many stub validations are held in flight together, and how many workers
# the test's default executor has. Two is also the archive-validation pool's
# size, so a barrier of this many trips there too.
IN_FLIGHT = 2

# How long a call on a free default executor may take before the test fails.
_PROMPT_SECONDS = 5


class InFlight:
    """:data:`IN_FLIGHT` stub validations that block together until released."""

    def __init__(self) -> None:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=IN_FLIGHT))
        self.all_running = asyncio.Event()
        self.release = threading.Event()

        def all_arrived() -> None:
            loop.call_soon_threadsafe(self.all_running.set)

        self._barrier = threading.Barrier(IN_FLIGHT, action=all_arrived)

    def block(self) -> None:
        """Wait, in a stub validation, for the others and then for the release."""

        self._barrier.wait(10)
        if not self.release.wait(10):
            raise AssertionError("the stub validation was never released")


async def assert_default_executor_free(in_flight: InFlight) -> None:
    """Wait until every stub is running, probe the default executor, release.

    The stubs are released on every path: the archive-validation pool is shared
    by every test in the process.
    """

    probe: asyncio.Future[int] | None = None
    try:
        await asyncio.wait_for(in_flight.all_running.wait(), 10)
        probe = asyncio.ensure_future(asyncio.to_thread(int))
        done, _ = await asyncio.wait({probe}, timeout=_PROMPT_SECONDS)
        assert done, "the default executor ran nothing while the validations ran"
    finally:
        in_flight.release.set()
        if probe is not None:
            await probe
