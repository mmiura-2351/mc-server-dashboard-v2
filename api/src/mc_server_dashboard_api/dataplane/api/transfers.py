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
import logging
import tarfile
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse

from mc_server_dashboard_api.dependencies import (
    ResolvedJarLookup,
    get_resolved_jar_lookup,
    get_storage,
    get_worker_credential,
)
from mc_server_dashboard_api.http_problem import problem
from mc_server_dashboard_api.storage.domain.errors import (
    IncompleteTransferError,
    IntegrityCheckError,
    MissingRegionsError,
    NotFoundError,
    StaleGenerationError,
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

# The response header carrying the authoritative working-set generation on both
# data-plane transfers (issue #763): the generation served on a hydrate, and the
# new generation published on a snapshot. The Worker records it as the generation
# its local scratch is at. Mirrors the constant the Worker reads (datatransfer.go).
_GENERATION_HEADER = "X-Working-Set-Generation"

# The REQUEST header a publishing Worker stamps with the store generation its
# working set was hydrated from (issue #847). The publish-time generation guard
# (defense-in-depth) refuses a publish whose base generation no longer matches the
# store's current generation: the set this Worker holds was hydrated from a now-
# superseded store state, so committing it would clobber a newer authoritative copy
# with stale progression. Absent (a Worker that never hydrated, or an older Worker
# not sending it) means "no claim to check" — the publish proceeds as before, so the
# header is backward-compatible. Mirrors the constant the Worker sends
# (datatransfer.go).
_BASE_GENERATION_HEADER = "X-Working-Set-Base-Generation"

# The REQUEST header carrying the publishing Worker's own id (issue #847 bug 3),
# recorded alongside the generation at commit so the guard can tell a same-Worker
# re-publish (lost-response self-heal) from a different-Worker stale-scratch publish
# (A->B->A). Absent means "publisher unknown" — the guard then cannot prove a foreign
# publisher and stays permissive. Mirrors the constant the Worker sends
# (datatransfer.go).
_WORKER_ID_HEADER = "X-Worker-Id"

router = APIRouter(prefix="/data-plane")

_logger = logging.getLogger(__name__)

# Cap the per-directory missing-region list surfaced in the 422 extensions and log
# line so a pathologically corrupt working set (every region of many dimensions
# dropped) cannot produce an unbounded problem body / log line. The operator only
# needs enough names to drive the documented recovery (delete the lost names from
# ``current/`` via the file API, then re-publish, STORAGE.md); the truncation flag
# tells them the list is partial so they don't trust it as exhaustive.
_MISSING_REGION_DIR_CAP = 20
_MISSING_REGION_NAME_CAP = 50

# Reject an implausibly large snapshot body before it can exhaust the disk. A
# generous M1 ceiling (a Minecraft working set is well under this); a real-world
# limit can be made configurable when a deployment needs it.
_MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024 * 1024  # 50 GiB

_BEARER_PREFIX = "Bearer "


def _bounded_missing_regions(
    exc: MissingRegionsError,
) -> tuple[list[dict[str, object]], bool]:
    """Build a BOUNDED per-directory list of lost region names for the 422/log.

    Returns ``(directories, truncated)`` where ``directories`` is at most
    ``_MISSING_REGION_DIR_CAP`` entries, each ``{"directory": <posix path>,
    "missing": [<name>, ...]}`` with the name list capped at
    ``_MISSING_REGION_NAME_CAP``. ``truncated`` is True when either cap dropped
    findings, so the caller can flag the list as partial. The report's findings
    are already sorted by directory (region.compare_region_name_sets), giving a
    stable, deterministic prefix.
    """

    truncated = False
    findings = exc.report.partial_loss
    if len(findings) > _MISSING_REGION_DIR_CAP:
        truncated = True
    directories: list[dict[str, object]] = []
    for finding in findings[:_MISSING_REGION_DIR_CAP]:
        names = list(finding.lost)
        if len(names) > _MISSING_REGION_NAME_CAP:
            truncated = True
            names = names[:_MISSING_REGION_NAME_CAP]
        directories.append(
            {"directory": finding.directory.as_posix(), "missing": names}
        )
    return directories, truncated


async def require_worker_credential(
    expected: Annotated[str | None, Depends(get_worker_credential)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Authenticate a data-plane request by the shared Worker credential.

    Mirrors the control-plane auth model (CONTROL_PLANE.md Section 4.1): a
    ``Authorization: Bearer <credential>`` header compared constant-time against
    ``control.worker_credential``, injected via ``Depends(get_worker_credential)``
    so a test can substitute it through ``dependency_overrides`` (issue #1753). A
    missing/wrong credential is a uniform 401. The credential is required to mount
    the data plane, enforced at app startup (the app factory fails fast); it is
    non-None here.
    """

    presented = _bearer(authorization)
    if (
        expected is None
        or presented is None
        or not hmac.compare_digest(presented, expected)
    ):
        raise problem(
            status.HTTP_401_UNAUTHORIZED,
            "worker_credential_rejected",
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

    # Stamp the authoritative working-set generation the hydrate serves (issue #763)
    # so the Worker records the generation its scratch is now at. 0 when no snapshot
    # has been published (the Worker then treats the set as older than any store
    # generation and re-hydrates later), matching the Worker's "nothing held"
    # default. The header rides every response, including the 204 (no working set).
    headers = {_GENERATION_HEADER: str(await storage.current_generation(*scope))}

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
                headers=headers,
            )
        # No published working set, but a JAR is resolved: send a tar with only the
        # JAR member so the Worker can still launch.
        return StreamingResponse(
            _single_member_tar(jar_member),
            media_type="application/x-tar",
            headers=headers,
        )
    if jar_member is None:
        return StreamingResponse(
            primed, media_type="application/x-tar", headers=headers
        )
    return StreamingResponse(
        _with_jar_member(primed, jar_member),
        media_type="application/x-tar",
        headers=headers,
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
    response: Response,
    storage: Annotated[Storage, Depends(get_storage)],
    content_length: Annotated[int | None, Header()] = None,
    base_generation: Annotated[
        int | None, Header(alias=_BASE_GENERATION_HEADER)
    ] = None,
    publisher: Annotated[str | None, Header(alias=_WORKER_ID_HEADER)] = None,
) -> None:
    """Stage and atomically publish the Worker's working set (snapshot, FR-DATA-4).

    The "proven complete" gate: a missing Content-Length is 411, an oversized one
    is 413, a streamed-byte count that does not match Content-Length is 400, and a
    body that stages zero files (an empty working set) is 400 ``empty_snapshot`` —
    in every reject path the staged transfer is aborted, so ``current/`` keeps the
    prior authoritative copy (FR-DATA-6, STORAGE.md Section 4.1).

    The cap and the declared length are both enforced *during* streaming, not only
    against the header: an under-declaring client (small Content-Length, longer
    body) or one that over-runs the cap is aborted as soon as the counted bytes
    cross the boundary, so a misdeclared length cannot spool the whole body to disk
    before the mismatch is caught.

    A publish-time generation guard (issue #847) refuses, BEFORE staging, a publish
    whose declared base generation (the store generation this Worker hydrated from)
    is OLDER than the store's current generation AND whose current was published by a
    DIFFERENT Worker: the set it holds was hydrated from a now-superseded state that
    another Worker has already advanced past, so committing it would clobber that
    newer authoritative copy with stale progression (the A->B->A stale-scratch case).

    Publisher identity is what keeps the guard from wedging a lost publish-response
    (issue #847 bug 3): a Worker that published generation N+1 but lost the HTTP
    response keeps its local marker at base N, so its next publish declares base
    N < current N+1 — but it is the SAME Worker that produced current, so the guard
    ALLOWS it and the publish self-heals (advancing the store again) instead of
    refusing forever. An absent base-generation header (never hydrated / older
    Worker), a base at or ahead of current, or an unknown current-publisher (an older
    publish that recorded no id) all skip the refusal — the guard stays permissive,
    and #847's primary fix (the API holds the assignment across the final snapshot)
    already prevents the genuine stale cross-worker publish from arising. The guard is
    defense-in-depth on the data plane, where the only prior protection was the
    Worker-side per-stream ctx cancel.

    The pre-stream guard runs ONCE, before the (multi-minute) upload stream, so an
    at-rest edit (issue #889) or a backup restore (issue #873) can land AFTER it
    passes and the commit would silently clobber that just-bumped ``current`` (issue
    #899). To close the upload window, the base the guard validated against — the
    store's ``current`` at guard time — is threaded into ``commit_snapshot`` as
    ``expected_base``; the commit re-reads the generation under the same per-server
    serialization the bump uses and refuses (409 ``stale_generation``, the same
    contract as the pre-stream refusal) when it advanced past that base. The staging
    is discarded and the newer ``current`` is kept, so the Worker re-bases on its next
    start — the same convergence as the pre-stream refusal.

    The content-integrity gate uses the single region rule set (issue #927): a
    non-4096-aligned tail is the normal on-disk shape of a 26.x world, not a tear, on
    every snapshot source. The earlier source-keyed strict/live split (issue #923)
    relied on a ``stopped => 4096-padded`` invariant that does not survive a
    sweep-stop timeout, SIGKILL, OOM, or crash — so the strict rule refused the
    stop-leg checkpoint exactly when it is the last chance to capture the world. The
    byte-precise check still catches realistic tears (a referenced chunk overrunning
    EOF, an entry past EOF, a severed prefix).
    """

    if content_length is None:
        raise problem(status.HTTP_411_LENGTH_REQUIRED, "content_length_required")
    if content_length > _MAX_SNAPSHOT_BYTES:
        raise problem(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "snapshot_too_large")

    scope = (CommunityId(community_id), ServerId(server_id))
    # Read the store's ``current`` once here (guard time) and ALWAYS thread it into
    # ``commit_snapshot`` as ``expected_base`` (issue #899/#920): the commit re-checks
    # it AFTER the upload stream, so an at-rest edit (issue #889) or backup restore
    # (issue #873) that advances the store during the (multi-minute) upload window is
    # caught at publish time. This re-check is derived purely server-side, so it
    # applies even to a publish that declares no base generation (older Worker / never
    # hydrated) — previously that path skipped the re-check and left the clobber window
    # open (#920 review). The port still accepts ``expected_base=None`` for callers
    # genuinely outside this upload guard (e.g. an initial publish / restore performed
    # by storage internals, which read ``current`` under the same lock themselves).
    current = await storage.current_generation(*scope)
    expected_base: int | None = current
    if base_generation is not None:
        if base_generation < current:
            # The publisher's working set was hydrated from generation
            # ``base_generation`` but the store has since moved to ``current``. Refuse
            # ONLY when current was published by a DIFFERENT Worker — that is the
            # A->B->A stale-scratch case where committing would clobber another
            # Worker's newer authoritative copy. When the SAME Worker (or an unknown
            # publisher) produced current, the lag is a lost publish-response and the
            # publish is allowed to self-heal (issue #847 bug 3). Nothing was staged
            # yet, so a refused ``current/`` is untouched.
            current_publisher = await storage.current_publisher(*scope)
            if (
                current_publisher is not None
                and publisher is not None
                and current_publisher != publisher
            ):
                _logger.warning(
                    "snapshot publish refused: stale base generation for server %s "
                    "from a different worker (publisher %s held %d, store at %d "
                    "published by %s)",
                    server_id,
                    publisher,
                    base_generation,
                    current,
                    current_publisher,
                )
                raise problem(
                    status.HTTP_409_CONFLICT,
                    "stale_generation",
                    extensions={
                        "base_generation": base_generation,
                        "current": current,
                    },
                )
    handle = await storage.begin_snapshot(*scope)
    counter = _ByteCounter(request.stream(), declared=content_length)
    try:
        await storage.write_snapshot(handle, counter.stream())
        if counter.count != content_length:
            # The transfer was truncated (or over-long): refuse to publish. Storage
            # only made staging authoritative on commit, so an abort leaves the
            # prior snapshot intact.
            await storage.abort_snapshot(handle)
            raise problem(status.HTTP_400_BAD_REQUEST, "length_mismatch")
        generation = await storage.commit_snapshot(
            handle,
            publisher=publisher,
            expected_base=expected_base,
        )
        # Stamp the new authoritative generation on the response (issue #763): the
        # Worker records the header as the generation its scratch is now at (the
        # source of this published snapshot). The reconciler reads the authoritative
        # value back from Storage directly, so there is no DB mirror to write here.
        response.headers[_GENERATION_HEADER] = str(generation)
    except HTTPException:
        raise
    except _CapExceeded:
        # Counted bytes ran past the 50 GiB cap mid-stream: abort and reject before
        # the disk fills. (An over-run that stays under the cap surfaces as the
        # length mismatch above.)
        await storage.abort_snapshot(handle)
        raise problem(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "snapshot_too_large"
        ) from None
    except _DeclaredLengthExceeded:
        # An under-declaring client streamed more than its Content-Length: abort
        # mid-stream rather than spooling the whole over-long body first.
        await storage.abort_snapshot(handle)
        raise problem(status.HTTP_400_BAD_REQUEST, "length_mismatch") from None
    except StaleGenerationError as exc:
        # The store advanced past the base the pre-stream guard validated against
        # DURING the upload window (issue #899): an at-rest edit or a backup restore
        # landed after the guard passed, so committing the just-uploaded staging would
        # clobber that newer authoritative copy with stale progression. commit_snapshot
        # already discarded the staging (the prior ``current`` keeps the newer copy, no
        # bump), exactly as the pre-stream guard leaves it untouched. Surface the SAME
        # 409 stale_generation contract the pre-stream refusal uses so the Worker
        # re-bases on its next start, and log the refusal in the same shape.
        # NOTE: unlike the pre-stream refusal (whose ``base_generation`` is the
        # Worker's DECLARED base), here ``base_generation`` carries the guard-time
        # ``current`` (= ``expected_base``) the commit re-checked against. Same field,
        # subtly different semantics: there is no worker-declared base on the no-base
        # path, and the value the Worker needs to re-base on is ``current`` anyway.
        _logger.warning(
            "snapshot publish refused: working-set generation advanced during upload "
            "for server %s (guard base %d, store now at %d)",
            server_id,
            exc.expected_base,
            exc.current,
        )
        raise problem(
            status.HTTP_409_CONFLICT,
            "stale_generation",
            extensions={
                "base_generation": exc.expected_base,
                "current": exc.current,
            },
        ) from None
    except IncompleteTransferError:
        # The body cleared the length gate but staged zero files: an empty working
        # set is not a publishable snapshot (STORAGE.md Section 4.1). A worker
        # packing an empty working dir is a bug signal, so reject it loudly; the
        # abort leaves the prior authoritative copy intact.
        await storage.abort_snapshot(handle)
        raise problem(status.HTTP_400_BAD_REQUEST, "empty_snapshot") from None
    except IntegrityCheckError as exc:
        # The byte-complete body staged a structurally corrupt working set (a
        # crash-during-save truncation, #703): the content-integrity gate (#739)
        # refused to publish it, so ``current`` keeps the prior good snapshot.
        # commit_snapshot already cleaned the corrupt staging area. Surface a clear
        # non-2xx with a machine-readable reason and the corrupt-file count so the
        # worker/operator sees *why* the publish was refused, and log the refusal.
        corrupt = len(exc.report.corrupt)
        _logger.warning(
            "snapshot publish refused: corrupt working set for server %s "
            "(%d corrupt region file(s))",
            server_id,
            corrupt,
        )
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "working_set_corrupt",
            extensions={"corrupt_count": corrupt},
        ) from None
    except MissingRegionsError as exc:
        # The byte-complete, structurally-clean body DROPPED region files from a
        # dimension that still exists (issue #854): MC would silently regenerate the
        # missing chunks, so the missing-region gate refused to publish and ``current``
        # keeps the prior good snapshot. commit_snapshot already cleaned the staging
        # area. A legitimate full-dimension delete is not flagged; this is a
        # partial-loss corruption signature, so surface it loudly and log it. The
        # documented recovery (STORAGE.md) requires the LOST NAMES — delete them from
        # ``current/`` via the file API at-rest, then re-publish — so carry a bounded
        # per-directory list in the extensions and the log line (capped so a
        # pathologically corrupt set cannot produce an unbounded body, flagged when
        # truncated).
        affected = len(exc.report.partial_loss)
        directories, truncated = _bounded_missing_regions(exc)
        _logger.warning(
            "snapshot publish refused: incomplete working set for server %s "
            "(%d dimension(s) lost region files; missing=%s%s)",
            server_id,
            affected,
            directories,
            " [truncated]" if truncated else "",
        )
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "working_set_incomplete",
            extensions={
                "affected_count": affected,
                "directories": directories,
                "truncated": truncated,
            },
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
