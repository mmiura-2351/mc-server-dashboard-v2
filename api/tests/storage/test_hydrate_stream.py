"""Hydrate stream failure-surfacing and lease lifecycle (STORAGE.md Section 4.2).

Two safety properties of the streaming hydrate egress that are easy to get
wrong:

1. **Errors must surface, never silently truncate.** If the producer thread
   fails part-way through the tar (e.g. an I/O error reading a working-set
   file), the consumer must see the stream END WITH AN ERROR, not a clean EOF
   the API would report as a successful (but truncated) hydrate.
2. **The active-reader lease must not leak.** The lease is acquired on the first
   iteration (not at ``open_hydrate_source`` call time), so a caller that opens
   the stream but never iterates/closes it never leases the snapshot, and a
   subsequent publish/sweep reclaims it normally.
"""

from __future__ import annotations

import gc
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

import pytest

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from tests.storage.helpers import (
    drain,
    new_scope,
    publish,
    read_tar,
    snapshot_dir,
)


async def test_producer_failure_surfaces_as_stream_error_not_silent_eof(
    tmp_path: Path,
) -> None:
    # A working-set member fails to be read while the tar is being produced. The
    # consumer must observe an error, not a clean (truncated) EOF.
    def _boom(_child: Path) -> None:
        raise OSError("injected read failure")

    storage = FsStorage(tmp_path, tar_member_hook=_boom)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})

    with pytest.raises(OSError, match="injected read failure"):
        await drain(storage.open_hydrate_source(community, server))


async def test_open_but_never_iterated_does_not_lease_snapshot(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"OLD"})
    old_snapshot = snapshot_dir(tmp_path, community, server)

    # Open the hydrate stream but NEVER iterate it, then drop the reference. The
    # lease is acquired on first iteration only, so nothing was ever leased.
    stream = storage.open_hydrate_source(community, server)
    del stream
    gc.collect()

    # A publish supersedes OLD; because OLD was never leased, reclaim removes it.
    await publish(storage, community, server, {"f": b"NEW"})
    assert not old_snapshot.exists()

    # Sweep (the other reclaim path) likewise has nothing held back.
    storage.sweep()
    assert not old_snapshot.exists()


async def test_first_chunk_then_publish_completes_old_content(
    tmp_path: Path,
) -> None:
    # Existing semantics preserved: once iteration has begun (lease taken), a
    # concurrent publish does not pull the snapshot out from under the reader.
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"OLD"})
    old_snapshot = snapshot_dir(tmp_path, community, server)

    stream = storage.open_hydrate_source(community, server)
    first_chunk = await stream.__anext__()

    await publish(storage, community, server, {"f": b"NEW"})
    assert old_snapshot.exists()  # leased, so reclaim deferred

    rest = await drain(stream)
    assert read_tar(first_chunk + rest) == {"f": b"OLD"}


async def test_aclose_releases_lease_and_sweep_reclaims(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"OLD"})
    old_snapshot = snapshot_dir(tmp_path, community, server)

    stream = cast(
        AsyncGenerator[bytes, None], storage.open_hydrate_source(community, server)
    )
    await stream.__anext__()  # begin iteration -> lease acquired

    await publish(storage, community, server, {"f": b"NEW"})
    assert old_snapshot.exists()  # leased

    await stream.aclose()  # releases the lease

    storage.sweep()
    assert not old_snapshot.exists()


# --- per-file streaming read leases (issue #265) ---------------------------
#
# open_file_stream takes the SAME active-reader lease open_hydrate_source does
# (the live snapshot it streams a file out of must not be reclaimed mid-read),
# so the same lease-lifecycle properties are asserted here.


async def test_open_file_stream_open_but_never_iterated_does_not_lease(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"OLD"})
    old_snapshot = snapshot_dir(tmp_path, community, server)

    # Open the file stream but NEVER iterate it, then drop the reference. The
    # lease is acquired on first iteration only, so nothing was ever leased.
    from mc_server_dashboard_api.storage.domain.value_objects import RelPath

    stream = storage.open_file_stream(community, server, RelPath("f"))
    del stream
    gc.collect()

    await publish(storage, community, server, {"f": b"NEW"})
    assert not old_snapshot.exists()  # never leased -> reclaim removed it


async def test_open_file_stream_first_chunk_then_publish_completes_old_content(
    tmp_path: Path,
) -> None:
    # Once iteration has begun (lease taken), a concurrent publish does not pull
    # the snapshot out from under the reader: the stream finishes the old bytes.
    from mc_server_dashboard_api.storage.domain.value_objects import RelPath

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"OLD"})
    old_snapshot = snapshot_dir(tmp_path, community, server)

    stream = cast(
        AsyncGenerator[bytes, None],
        storage.open_file_stream(community, server, RelPath("f")),
    )
    first_chunk = await stream.__anext__()  # begin iteration -> lease acquired

    await publish(storage, community, server, {"f": b"NEW"})
    assert old_snapshot.exists()  # leased, so reclaim deferred

    rest = await drain(stream)
    assert first_chunk + rest == b"OLD"


async def test_open_file_stream_aclose_releases_lease_and_sweep_reclaims(
    tmp_path: Path,
) -> None:
    # Mid-iteration release: closing the stream before EOF must release the lease
    # so a later sweep reclaims the superseded snapshot.
    from mc_server_dashboard_api.storage.domain.value_objects import RelPath

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    # A multi-chunk file so the stream is still mid-iteration when we aclose it.
    big = b"z" * (3 * 1024 * 1024)
    await publish(storage, community, server, {"f": big})
    old_snapshot = snapshot_dir(tmp_path, community, server)

    stream = cast(
        AsyncGenerator[bytes, None],
        storage.open_file_stream(community, server, RelPath("f")),
    )
    await stream.__anext__()  # begin iteration -> lease acquired (still mid-file)

    await publish(storage, community, server, {"f": b"NEW"})
    assert old_snapshot.exists()  # leased

    await stream.aclose()  # mid-iteration release

    storage.sweep()
    assert not old_snapshot.exists()
