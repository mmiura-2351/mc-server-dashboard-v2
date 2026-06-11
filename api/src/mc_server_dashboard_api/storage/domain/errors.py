"""Domain errors for the storage context.

Raised by the :class:`~.port.Storage` Port's value objects and adapters on
invariant or policy violations (a rejected traversal path, a missing key, an
incomplete transfer refused at publish). They carry no framework type and are
translated to transport errors at the edge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mc_server_dashboard_api.storage.integrity.region import (
        MissingRegionReport,
        WorkingSetReport,
    )


class StorageError(Exception):
    """Base class for storage-context invariant/policy violations."""


class PathTraversalError(StorageError):
    """A caller-supplied ``rel_path`` escaped (or could escape) the server root.

    Raised by :class:`~.value_objects.RelPath` validation and by the adapter's
    canonicalize-then-contain check (STORAGE.md Section 6) for absolute paths,
    ``..`` components, or a symlink that resolves outside the server's
    ``current/`` root. The rejection is explicit, never a silent clamp.
    """


class NotFoundError(StorageError):
    """The targeted blob does not exist.

    Raised by reads (file, file version, JAR, hydrate source) and by
    backup/restore when the key/path/version is unknown for the given scope.
    """


class ArchiveTooLargeError(StorageError):
    """A backup archive's members inflate past the restore decompressed-size cap.

    The compressed archive body is bounded on the way in, but a gzip member can
    expand ~1000x; restore extraction counts the cumulative DECOMPRESSED bytes and
    refuses an archive that exceeds the adapter's ``max_restore_bytes`` before it
    can fill the disk (gzip-bomb defence, issue #287). The bound is over actual
    bytes read, so a member that under-reports its header size cannot slip past.
    """


class IncompleteTransferError(StorageError):
    """A snapshot commit was attempted without a proven-complete transfer.

    ``commit_snapshot`` refuses to publish a staging area that the data plane has
    not signalled complete (STORAGE.md Section 4.1); publishing a partial copy is
    the exact defect the atomic-publish protocol forbids (FR-DATA-6).

    The same gate also refuses an *empty* staging area even when the transfer
    completed cleanly: a worker packing an empty working set is a bug signal,
    never a valid snapshot. The snapshot endpoint surfaces this as
    ``400 empty_snapshot`` (STORAGE.md Section 8).
    """


class SnapshotHandleError(StorageError):
    """A snapshot handle was used out of its valid lifecycle.

    Raised when a handle is reused after commit/abort, or when its staging area
    has vanished (already aborted or swept). Keeps the two-phase protocol honest.
    """


class StaleGenerationError(StorageError):
    """The store advanced past the publisher's base during the upload window (#899).

    The data-plane publish guard evaluates the base-generation claim ONCE, before
    the (multi-minute) upload stream. ``commit_snapshot`` re-checks it: if the
    authoritative generation advanced past the base the guard validated against
    (an at-rest edit or a backup restore landed AFTER the guard passed), publishing
    the just-uploaded staging would silently clobber that newer authoritative copy
    with stale progression. The commit refuses fail-closed — the staging is
    discarded and ``current`` keeps the newer copy — so the Worker re-bases on its
    next start (the same convergence as the pre-stream refusal). The re-check runs
    under the same per-server serialization the generation bump uses, so an edit
    cannot interleave between the re-check and the pointer flip.

    Carries the ``expected_base`` the guard validated against and the ``current``
    the re-check observed, so the edge can surface the same machine-readable
    ``409 stale_generation`` contract the pre-stream guard uses.
    """

    def __init__(self, expected_base: int, current: int) -> None:
        self.expected_base = expected_base
        self.current = current
        super().__init__(
            f"working-set generation advanced during upload: base {expected_base}, "
            f"store now at {current}"
        )


class IntegrityCheckError(StorageError):
    """A working set failed the structural ``.mca`` integrity gate (issue #739).

    The authoritative-create direction is fail-closed: before a snapshot is
    published and before a backup archive is written, the staged/current working
    set is walked for structurally corrupt region files (issue #738). Any corrupt
    ``.mca`` refuses the operation so a crash-corrupted world cannot poison the
    published snapshot or a new backup; the prior ``current`` is left untouched
    (STORAGE.md, #703 last-known-good retention).

    Carries the structured :class:`~...integrity.region.WorkingSetReport` so a
    caller can surface *why* — the corrupt-file count and per-file reason codes.
    """

    def __init__(self, report: WorkingSetReport) -> None:
        self.report = report
        corrupt = len(report.corrupt)
        super().__init__(
            f"working set failed integrity check: {corrupt} corrupt region file(s)"
        )


class MissingRegionsError(StorageError):
    """A publish would drop SOME-but-not-ALL region files of a live dimension (#854).

    The structural ``.mca`` gates (#739/#749) validate only files that EXIST, so a
    vanished region file is structurally valid absence: it publishes/restores fine
    and Minecraft silently regenerates the chunks. This gate compares the staged
    region-file set against the prior ``current/`` set per region-bearing directory
    and refuses fail-closed when a directory that still has regions LOST some of
    them (the partial-loss corruption signature). A directory whose regions are ALL
    gone is a legitimate full-dimension/world delete and is allowed — so deleting a
    dimension is the documented override path for an intentional shrink; only a
    partial loss is refused, leaving ``current`` untouched (last-known-good, #703).

    Carries the structured :class:`~...integrity.region.MissingRegionReport` so a
    caller can surface *which* directories lost *which* region files.
    """

    def __init__(self, report: MissingRegionReport) -> None:
        self.report = report
        affected = len(report.partial_loss)
        super().__init__(
            f"working set is incomplete: {affected} dimension(s) lost region files"
        )
