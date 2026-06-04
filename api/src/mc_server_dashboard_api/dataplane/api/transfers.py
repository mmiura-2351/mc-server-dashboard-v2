"""Worker-authenticated data-plane HTTP endpoints (issue #106).

The control plane triggers a transfer; the bulk bytes ride here. Two endpoints,
both scoped by ``(community_id, server_id)`` and both authenticated by the shared
Worker credential (Bearer, constant-time compare) — the same credential the
control-plane gRPC stream uses (CONTROL_PLANE.md Section 4.1, NFR-SEC-1). They are
never community-authenticated: a Worker is platform infrastructure, not a member.

- ``GET .../working-set`` streams the authoritative working set as a tar
  (hydrate). The resolved server JAR is injected as a ``server.jar`` tar member
  when the server has one recorded and it is present in the pool (issue #118);
  otherwise the working set is sent alone. A server with neither a published
  snapshot nor a resolved JAR is **204 No Content** (the Worker treats it as an
  empty working set and starts fresh), distinct from a never-existing scope; a
  resolved JAR with no snapshot is a ``200`` tar carrying just ``server.jar``.
- ``POST .../snapshot`` streams the Worker's tar into staging and atomically
  publishes it (snapshot). The "proven complete" gate (STORAGE.md Section 4.1):
  the request MUST carry a ``Content-Length`` and the streamed byte count MUST
  match it, or the staged transfer is aborted and never published — Storage's
  staging guarantees a partial upload is never made authoritative (FR-DATA-6). A
  size cap rejects an implausibly large body before it can fill the disk.

The archive format is the stdlib tar stream Storage already produces/consumes
(STORAGE.md Section 7.1 wire-format note); this surface carries it verbatim.
"""

from __future__ import annotations

import hmac
import io
import tarfile
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from mc_server_dashboard_api.dependencies import (
    ResolvedJarLookup,
    get_resolved_jar_lookup,
    get_settings,
    get_storage,
)
from mc_server_dashboard_api.storage.domain.errors import (
    IncompleteTransferError,
    NotFoundError,
)
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    JarKey,
    ServerId,
)

# The conventional relpath the resolved server JAR is injected at in the hydrate
# tar; the StartServer command launches the Worker against this path
# (servers/application/lifecycle.py ``_DEFAULT_JAR_RELPATH``).
_JAR_RELPATH = "server.jar"
_TAR_BLOCK = 512

router = APIRouter(prefix="/data-plane")

# Reject an implausibly large snapshot body before it can exhaust the disk. A
# generous M1 ceiling (a Minecraft working set is well under this); a real-world
# limit can be made configurable when a deployment needs it.
_MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024 * 1024  # 50 GiB

_BEARER_PREFIX = "Bearer "


async def require_worker_credential(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Authenticate a data-plane request by the shared Worker credential.

    Mirrors the control-plane auth model (CONTROL_PLANE.md Section 4.1): a
    ``Authorization: Bearer <credential>`` header compared constant-time against
    ``control.worker_credential``. A missing/wrong credential is a uniform 401.
    The credential is required to mount the data plane, enforced at app startup
    (the app factory fails fast); it is non-None here.
    """

    settings = get_settings(request)
    expected = settings.control.worker_credential
    presented = _bearer(authorization)
    if (
        expected is None
        or presented is None
        or not hmac.compare_digest(presented, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="worker_credential_rejected",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _bearer(authorization: str | None) -> str | None:
    if authorization is None or not authorization.startswith(_BEARER_PREFIX):
        return None
    return authorization[len(_BEARER_PREFIX) :]


@router.get(
    "/communities/{community_id}/servers/{server_id}/working-set",
    dependencies=[Depends(require_worker_credential)],
)
async def hydrate_working_set(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    storage: Annotated[Storage, Depends(get_storage)],
    resolved_jar: Annotated[ResolvedJarLookup, Depends(get_resolved_jar_lookup)],
) -> StreamingResponse:
    """Stream the authoritative working set + resolved JAR as a tar (hydrate).

    Closes the M1 JAR slot (STORAGE.md Section 8, ARCHITECTURE.md Section 7.3): when
    the server has a resolved JAR recorded (start ensured it into the pool), it is
    injected into the hydrate tar at the conventional ``server.jar`` relpath so the
    Worker launches against it without ever fetching JARs itself (FR-VER-3). The
    JAR is serialised as one EOF-less tar member and prepended before the working
    set, which keeps its own trailing end-of-archive marker, so the concatenation
    is a single valid archive.

    The 204 posture is preserved only when there is *nothing* to send — no
    published snapshot and no resolved JAR. With a resolved JAR but no snapshot
    (e.g. a never-started-then-started fresh server), the body is a tar carrying
    just ``server.jar`` so the Worker can still launch.
    """

    scope = (CommunityId(community_id), ServerId(server_id))
    jar_member = await _jar_member(storage, resolved_jar, community_id, server_id)

    stream = storage.open_hydrate_source(*scope)
    # The hydrate stream resolves + leases the snapshot on its FIRST iteration,
    # so a NotFoundError (no published snapshot) only surfaces once we pull a
    # chunk. Peek the first chunk here, before sending response headers, so an
    # unpublished server becomes a clean 204 rather than a stream that aborts
    # mid-body with headers already committed.
    try:
        primed = await _prime(stream)
    except NotFoundError:
        if jar_member is None:
            return StreamingResponse(
                _empty(),
                status_code=status.HTTP_204_NO_CONTENT,
                media_type="application/x-tar",
            )
        # No published working set, but a JAR is resolved: send a tar with only the
        # JAR member so the Worker can still launch.
        return StreamingResponse(
            _single_member_tar(jar_member), media_type="application/x-tar"
        )
    if jar_member is None:
        return StreamingResponse(primed, media_type="application/x-tar")
    return StreamingResponse(
        _with_jar_member(primed, jar_member), media_type="application/x-tar"
    )


async def _jar_member(
    storage: Storage,
    resolved_jar: ResolvedJarLookup,
    community_id: uuid.UUID,
    server_id: uuid.UUID,
) -> bytes | None:
    """Build the ``server.jar`` tar member bytes (header + content, no EOF).

    Returns ``None`` when the server has no resolved JAR recorded, or the recorded
    JAR is no longer in the pool (then the working set is sent alone, the prior
    posture). The JAR is read fully here: an M1 server JAR is tens of MB, within
    memory, which keeps the splice simple; the working set itself still streams.
    """

    sha256 = await resolved_jar(community_id, server_id)
    if sha256 is None:
        return None
    key = JarKey(sha256)
    if not await storage.has_jar(key):
        return None
    data = b"".join([chunk async for chunk in storage.open_jar(key)])
    return _tar_member_bytes(_JAR_RELPATH, data)


@router.post(
    "/communities/{community_id}/servers/{server_id}/snapshot",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_worker_credential)],
)
async def publish_snapshot(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    request: Request,
    storage: Annotated[Storage, Depends(get_storage)],
    content_length: Annotated[int | None, Header()] = None,
) -> None:
    """Stage and atomically publish the Worker's working set (snapshot, FR-DATA-4).

    The "proven complete" gate: a missing Content-Length is 411, an oversized one
    is 413, and a streamed-byte count that does not match Content-Length is 400 —
    in every reject path the staged transfer is aborted, so ``current/`` keeps the
    prior authoritative copy (FR-DATA-6, STORAGE.md Section 4.1).

    The cap and the declared length are both enforced *during* streaming, not only
    against the header: an under-declaring client (small Content-Length, longer
    body) or one that over-runs the cap is aborted as soon as the counted bytes
    cross the boundary, so a misdeclared length cannot spool the whole body to disk
    before the mismatch is caught.
    """

    if content_length is None:
        raise HTTPException(
            status_code=status.HTTP_411_LENGTH_REQUIRED,
            detail="content_length_required",
        )
    if content_length > _MAX_SNAPSHOT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="snapshot_too_large",
        )

    scope = (CommunityId(community_id), ServerId(server_id))
    handle = await storage.begin_snapshot(*scope)
    counter = _ByteCounter(request.stream(), declared=content_length)
    try:
        await storage.write_snapshot(handle, counter.stream())
        if counter.count != content_length:
            # The transfer was truncated (or over-long): refuse to publish. Storage
            # only made staging authoritative on commit, so an abort leaves the
            # prior snapshot intact.
            await storage.abort_snapshot(handle)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="length_mismatch",
            )
        await storage.commit_snapshot(handle)
    except HTTPException:
        raise
    except _CapExceeded:
        # Counted bytes ran past the 50 GiB cap mid-stream: abort and reject before
        # the disk fills. (An over-run that stays under the cap surfaces as the
        # length mismatch above.)
        await storage.abort_snapshot(handle)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="snapshot_too_large",
        ) from None
    except _DeclaredLengthExceeded:
        # An under-declaring client streamed more than its Content-Length: abort
        # mid-stream rather than spooling the whole over-long body first.
        await storage.abort_snapshot(handle)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="length_mismatch",
        ) from None
    except IncompleteTransferError:
        await storage.abort_snapshot(handle)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="incomplete_transfer"
        ) from None
    except BaseException:
        # Any failure mid-transfer (client disconnect, extraction error) discards
        # staging; the authoritative copy is never touched.
        await storage.abort_snapshot(handle)
        raise


class _CapExceeded(Exception):
    """Counted bytes ran past the absolute snapshot cap mid-stream."""


class _DeclaredLengthExceeded(Exception):
    """Counted bytes ran past the client's declared Content-Length mid-stream."""


class _ByteCounter:
    """Tee a request body stream, tallying the bytes that pass through.

    The tally is checked against the absolute cap and the client's declared length
    on every chunk: an under-declaring (or runaway) client is stopped as soon as it
    crosses a boundary, before the over-long body can be spooled to disk in full.
    """

    def __init__(self, source: AsyncIterator[bytes], declared: int) -> None:
        self._source = source
        self._declared = declared
        self.count = 0

    async def stream(self) -> AsyncIterator[bytes]:
        async for chunk in self._source:
            self.count += len(chunk)
            if self.count > _MAX_SNAPSHOT_BYTES:
                raise _CapExceeded
            if self.count > self._declared:
                raise _DeclaredLengthExceeded
            yield chunk


async def _prime(stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Pull the first chunk now (surfacing NotFoundError), then replay the rest."""

    iterator = stream.__aiter__()
    first = await iterator.__anext__()

    async def _replay() -> AsyncIterator[bytes]:
        yield first
        async for chunk in iterator:
            yield chunk

    return _replay()


async def _empty() -> AsyncIterator[bytes]:
    return
    yield  # pragma: no cover - makes this an async generator


def _tar_member_bytes(name: str, data: bytes) -> bytes:
    """Serialise one tar member (header + content + padding), WITHOUT the EOF marker.

    A complete archive ends with at least two zero blocks; this returns just the
    member so it can be concatenated *before* the working-set tar (which carries its
    own EOF), yielding a single valid archive without having to strip the working
    set's trailing zero blocks (which could collide with a member's zero content).
    """

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    # tarfile appends two zero blocks (EOF) plus record padding after the member;
    # the member itself occupies one header block + its content rounded up to a
    # block boundary. Slice to exactly that so no EOF marker lands mid-archive.
    member_len = _TAR_BLOCK + _round_up(len(data), _TAR_BLOCK)
    return raw[:member_len]


def _round_up(value: int, block: int) -> int:
    return ((value + block - 1) // block) * block


async def _single_member_tar(member: bytes) -> AsyncIterator[bytes]:
    """Emit a complete tar carrying one pre-serialised member (member + EOF)."""

    yield member
    yield b"\x00" * (_TAR_BLOCK * 2)


async def _with_jar_member(
    working_set: AsyncIterator[bytes], jar_member: bytes
) -> AsyncIterator[bytes]:
    """Prepend the JAR member, then stream the working-set tar verbatim.

    The JAR member carries no EOF, and the working-set tar keeps its own, so the
    concatenation is a single valid archive: ``server.jar`` first, then the working
    set's members, then one end-of-archive marker.
    """

    yield jar_member
    async for chunk in working_set:
        yield chunk
