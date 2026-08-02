"""A minimal in-memory S3 stub for the object-adapter tests (STORAGE.md Section 7.3).

Implements exactly the :class:`~...adapters.object_store.S3Client` surface the
adapter uses, backed by a shared ``dict`` of key -> bytes. A single store is
shared across every client the factory hands out (one bucket), so reads see
prior writes (read-after-write) and the pointer-flip / staged-upload / sweep
behaviour can be proven without any real cloud.

moto was rejected in favour of this stub: moto pulls in the full boto3/botocore
+ werkzeug/responses dependency tree and a heavier startup, for behaviour this
test suite only needs a handful of object ops to exercise. A ~70-line dict-backed
stub is simpler, has no extra dependency to pin under the 7-day cooldown, and
makes the failure-injection seams (used by the crash-safety tests) trivial.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mc_server_dashboard_api.storage.adapters.object_store import (
    MultipartUploadsUnsupportedError,
    S3Client,
    S3ClientFactory,
    S3MultipartUpload,
    S3Object,
)
from mc_server_dashboard_api.storage.domain.errors import (
    NotFoundError,
    ObjectStoreUnavailableError,
)

# Store time reported for an object seeded straight into ``objects`` without a
# stamp. Fixed and far future so the JAR-pool (#293) and plugin-cache (#1332) GC
# safety windows spare it under *any* clock: a test that seeds an object and
# forgets the stamp then reddens on the delete it was not asserting, instead of
# passing by accident. Reading the host clock here made that fail-loud property
# depend on the host clock running later than the caller's ``now`` minus the
# window — an unstated environmental assumption (#2529). Spelled identically in
# ``tests/versions/fakes.py`` and ``tests/servers/fakes.py``: three copies, one
# beside each store-time dict, because ``api/tests/`` has no shared helper module
# to hold it (only ``tests/conftest.py``). PR #2540's review weighed that at two
# copies and PR #2573's at three; both accepted. A fourth copy is the trigger to
# stop copying: extract the constant to ``tests/store_times.py`` and import it at
# all four sites (issue #2576).
_UNSTAMPED_STORE_TIME = dt.datetime(9999, 1, 1, tzinfo=dt.UTC)

# Write stamps read the host clock. That is the standing convention for every
# write in this fake and in the two sibling fakes (``tests/versions/fakes.py``,
# ``tests/servers/fakes.py``), recorded here once rather than at all five stamps
# (issue #2576):
#
# - Not the sentinel above. It means "no store time for this key", so a write
#   reusing it would also make it mean "written just now": an object written
#   through the fake would report the year 9999 and silently disable the GC
#   safety window the test meant to exercise — the fail-loud direction #2529
#   established, inverted. PR #2540 added the siblings' host-clock ``put`` stamps
#   in the same commit that replaced their read-side wall-clock fallback with the
#   sentinel, and PR #2573 left the stamps here (in place since #295) untouched
#   when it brought the sentinel over: the split is the design, not an oversight.
# - Not a fixed constant either, and that is what the pins say:
#   ``tests/versions/test_jar_pool_fake.py`` and
#   ``tests/servers/test_plugin_cache_store_fake.py`` bracket the siblings'
#   ``put`` between two host-clock reads, so any constant reddens them, a past
#   one included. ``tests/versions/test_ensure_jar.py``'s ``_PastClock``
#   (``now() + GC_SAFETY_WINDOW + 1h``) is a weaker, end-to-end check on top: it
#   stops ageing the JAR the test just put once the stamp is the sentinel, but a
#   past constant would slip by it.
# - Those pins cover the sibling ``put``s, not the three stamps below: changing
#   these reddens no test at all (mutation-checked at #2576), so this comment is
#   the only thing holding them.
# - Revisit if a GC test ever writes through a fake instead of seeding the store
#   time and pins its clock to a *constant* ahead of the host clock: it would
#   read the fresh stamp as ancient and pass for the wrong reason. None does
#   today (checked at #2576). ``tests/versions/test_ensure_jar.py`` does run the
#   GC ahead of the host clock over a ``put``-stamped JAR, but derives that clock
#   from ``now()`` and means the ageing, so it passes for its own reason.


class FakeS3Store:
    """The shared bucket contents: an ordered key -> bytes map.

    ``multipart_parts`` records, per key, how many parts the last multipart upload
    consumed. A client-side regression that buffers a stream whole before uploading
    would record a single part, so the per-part streaming contract (Section 7.3) is
    observable in tests.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.multipart_parts: dict[str, int] = {}
        # Per-key count of completed multipart uploads. Unlike ``multipart_parts``
        # (which records only the last upload's part count, so re-uploading
        # identical content leaves it unchanged), this tally rises on every
        # upload — letting a dedup-skip test prove no second upload occurred.
        self.upload_calls: dict[str, int] = {}
        # Per-key store time, mirroring S3 ``LastModified`` for the JAR-pool GC
        # safety window (#293). Set on every write, as the real backend stamps an
        # object it stores. A key a test seeded directly into ``objects`` has
        # none — see :data:`_UNSTAMPED_STORE_TIME` (issue #2542), where the
        # host-clock convention the write stamps follow is recorded too (#2576).
        self.mtimes: dict[str, dt.datetime] = {}
        # In-progress multipart uploads keyed by upload id, each a
        # (key, initiated) pair — the orphan-part sweep input (issue #903). Tests
        # seed leftovers here; ``abort_multipart_upload`` removes them. When
        # ``list_multipart_uploads_unsupported`` is set the stub raises
        # MultipartUploadsUnsupportedError to model a backend without the operation.
        self.multipart_uploads: dict[str, tuple[str, dt.datetime]] = {}
        self.list_multipart_uploads_unsupported = False
        # Read-path damage injection for the archive readability probe (#2371).
        # ``declared_sizes`` overrides what ``head_object`` reports for a key, so a
        # body can be delivered short of the length the store declares.
        # ``read_aborts`` queues, per key, one entry per ``get_object`` attempt: a
        # byte offset at which the body stream raises ObjectStoreUnavailableError
        # (the mid-stream connection teardown), or ``None`` to deliver in full.
        # An exhausted (or absent) queue delivers in full.
        self.declared_sizes: dict[str, int] = {}
        self.read_aborts: dict[str, list[int | None]] = {}


class FakeS3Client:
    """An :class:`S3Client` over a shared :class:`FakeS3Store`."""

    def __init__(self, store: FakeS3Store) -> None:
        self._store = store

    async def get_object(
        self, key: str, byte_range: tuple[int, int] | None = None
    ) -> AsyncIterator[bytes]:
        if key not in self._store.objects:
            raise NotFoundError(f"object not found: {key}")
        data = self._store.objects[key]
        if byte_range is not None:
            # The S3 ``Range`` parameter: an inclusive (first, last) pair, and
            # only those bytes come back (issue #2372).
            first, last = byte_range
            data = data[first : last + 1]
        queued = self._store.read_aborts.get(key)
        abort_at = queued.pop(0) if queued else None

        async def _gen() -> AsyncIterator[bytes]:
            if abort_at is not None:
                # Deliver up to the injected offset, then tear the body down the way
                # the real client does on a mid-stream connection abort (#2371).
                if abort_at:
                    yield data[:abort_at]
                raise ObjectStoreUnavailableError(f"object store read failed: {key}")
            # Yield in two chunks when large enough so streaming consumers see
            # more than one yield (the bounded-memory contract).
            half = len(data) // 2
            if half:
                yield data[:half]
                yield data[half:]
            else:
                yield data

        return _gen()

    async def put_object(self, key: str, body: bytes) -> None:
        self._store.objects[key] = bytes(body)
        self._store.mtimes[key] = dt.datetime.now(dt.UTC)

    async def upload_multipart(self, key: str, parts: AsyncIterator[bytes]) -> None:
        # Consume part-by-part (never a single whole-body read) and tally the parts,
        # so a client that buffers the stream whole — collapsing to one part — is
        # caught by the bounded-memory assertion in the tests (Section 7.3).
        buf = bytearray()
        count = 0
        async for chunk in parts:
            buf.extend(chunk)
            count += 1
        self._store.objects[key] = bytes(buf)
        self._store.multipart_parts[key] = count
        self._store.upload_calls[key] = self._store.upload_calls.get(key, 0) + 1
        self._store.mtimes[key] = dt.datetime.now(dt.UTC)

    async def head_object(self, key: str) -> int | None:
        obj = self._store.objects.get(key)
        if obj is None:
            return None
        return self._store.declared_sizes.get(key, len(obj))

    async def copy_object(self, src_key: str, dst_key: str) -> None:
        if src_key not in self._store.objects:
            raise NotFoundError(f"object not found: {src_key}")
        self._store.objects[dst_key] = self._store.objects[src_key]
        self._store.mtimes[dst_key] = dt.datetime.now(dt.UTC)

    async def delete_object(self, key: str) -> None:
        self._store.objects.pop(key, None)
        self._store.mtimes.pop(key, None)

    async def list_objects(self, prefix: str) -> list[S3Object]:
        return [
            S3Object(
                key=key,
                size=len(data),
                last_modified=self._store.mtimes.get(key, _UNSTAMPED_STORE_TIME),
            )
            for key, data in sorted(self._store.objects.items())
            if key.startswith(prefix)
        ]

    async def list_multipart_uploads(self, prefix: str) -> list[S3MultipartUpload]:
        if self._store.list_multipart_uploads_unsupported:
            raise MultipartUploadsUnsupportedError("stub: operation unsupported")
        return [
            S3MultipartUpload(key=key, upload_id=upload_id, initiated=initiated)
            for upload_id, (key, initiated) in sorted(
                self._store.multipart_uploads.items()
            )
            if key.startswith(prefix)
        ]

    async def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        # Honour ``key`` like real S3 (issue #935): a key/upload_id mismatch leaves the
        # upload in place — the real backend would return NoSuchUpload there, which the
        # adapter's idempotent translation turns into a no-op. Stays idempotent on a
        # matching pair (pop a present id; absent id is a no-op).
        if self._store.multipart_uploads.get(upload_id, (None, None))[0] == key:
            self._store.multipart_uploads.pop(upload_id, None)


def fake_s3_factory(store: FakeS3Store) -> S3ClientFactory:
    """A client factory yielding fresh :class:`FakeS3Client`s over one store."""

    @asynccontextmanager
    async def _factory() -> AsyncIterator[S3Client]:
        yield FakeS3Client(store)

    return _factory


class _AfterCloseUseError(AssertionError):
    """Raised when the adapter calls a client after its context manager exited."""


class _CloseTrackingClient:
    """Wrap an :class:`S3Client`, refusing every call made after close.

    A factory hands one of these out per ``async with``; once the context exits,
    any further method call on the (now dead) client raises — exactly the
    use-after-close that, with the real aioboto3 client, leaks an aiohttp
    ClientSession/connector on every snapshot publish (issue #948).
    """

    def __init__(self, inner: S3Client) -> None:
        self._inner = inner
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        async def _guarded(*args: object, **kwargs: object) -> object:
            if self._closed:
                raise _AfterCloseUseError(
                    f"client.{name} called after the client context closed"
                )
            return await attr(*args, **kwargs)

        return _guarded


def close_tracking_factory(inner: S3ClientFactory) -> S3ClientFactory:
    """Wrap a client factory so every yielded client raises on use-after-close.

    Used by the shared test harness to guard ALL adapter tests against the
    use-after-close class (issue #952).
    """

    @asynccontextmanager
    async def _factory() -> AsyncIterator[S3Client]:
        async with inner() as real:
            tracker = _CloseTrackingClient(real)
            try:
                yield tracker
            finally:
                tracker.close()

    return _factory
