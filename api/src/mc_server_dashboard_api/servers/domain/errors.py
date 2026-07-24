"""Domain errors for the servers context.

Raised by the pure domain (value objects, entities, use-case policy) on invariant
or policy violations. They carry no framework type and are translated to transport
errors at the edge.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


class ServerError(Exception):
    """Base class for servers-domain invariant/policy violations."""


class InvalidServerNameError(ServerError):
    """A server name failed its validation rules (e.g. blank)."""


class InvalidServerFieldError(ServerError):
    """A required server text field (edition, version, type) was blank."""


class UnknownServerTypeError(ServerError):
    """The ``server_type`` is outside the supported M1 catalog (CHECK enum)."""


class UnsupportedEditionError(ServerError):
    """The ``mc_edition`` is not supported at M1.

    The version catalog is Java-edition-only at M1 (Bedrock has no catalogued
    source), so create accepts ``mc_edition == 'java'`` and rejects anything else.
    The edge maps this to a 422.
    """


class ServerNotFoundError(ServerError):
    """The targeted server does not exist in the community.

    Raised by read/update/delete when the id is unknown or, security-critically,
    belongs to a *different* community (cross-community access): reported as
    not-found so no signal about another community's servers leaks (FR-COMM-3).
    """


class ServerNameAlreadyExistsError(ServerError):
    """Creation/rename hit the per-community server name uniqueness constraint."""


class PermissionDeniedError(ServerError):
    """A server update was denied because the caller lacks a required permission.

    ``UpdateServer`` gates every edit — a name, port, slug, or any config key — on
    ``server:update``. The missing permission is named in :attr:`permission` so
    the edge can carry it in the 403 ``permission`` member.
    """

    def __init__(self, permission: str) -> None:
        super().__init__(permission)
        self.permission = permission


class PortOutOfRangeError(ServerError):
    """An explicit ``game_port`` at create fell outside the configured range (#243).

    The configurable assignable range is ``ports.range_start..range_end``
    (CONFIGURATION.md Section 5.6); a port below or above it is rejected before the
    row is staged. The edge maps this to 422.
    """


class PortAlreadyTakenError(ServerError):
    """An explicit ``game_port`` at create is already held by another server (#243).

    Game ports are unique deployment-wide; a requested port already assigned is a
    conflict, distinct from an out-of-range value. The edge maps this to 409.
    """


class PortRangeExhaustedError(ServerError):
    """Auto-assignment found no free port in the configured range (issue #243).

    Every port in ``ports.range_start..range_end`` is already taken, so create
    cannot assign one. A transient capacity condition (freeing/deleting a server
    releases a port), so the edge maps this to 503.
    """


class WorkingSetSeedFailedError(ServerError):
    """The create-time working-set seeding step hit a storage failure (issue #243).

    The server row has already committed; only the seed write (eula.txt /
    server.properties) failed. The row is left in place -- a degraded but
    repairable state, since the operator can write the missing files via the files
    API -- and the failure is surfaced as a mapped 503 ``seed_failed`` rather than
    an unmapped 500. The edge logs a WARN at the create route.
    """


class InvalidSnapshotIntervalError(ServerError):
    """A per-server snapshot-interval override was invalid (FR-DATA-7).

    The override (``config['snapshot_interval_seconds']``) must be a positive
    integer at least ``snapshot.min_interval_seconds`` (the thrash floor,
    CONFIGURATION.md Section 5.4). A non-integer or below-floor value is rejected;
    the edge maps this to 422.
    """


class InvalidMemoryLimitError(ServerError):
    """A per-server memory limit was invalid (per-server resources, #705).

    The limit (``config['memory_limit_mb']``, in mebibytes) must be a positive
    integer (``bool`` rejected) within the accepted range: at least
    ``MEMORY_LIMIT_FLOOR_MB`` (a Minecraft server needs real heap) and no more
    than ``MEMORY_LIMIT_CEILING_MB`` (an absurd value is a typo, not an intent).
    The edge maps this to 422.
    """


class InvalidCpuAllocationError(ServerError):
    """A per-server CPU allocation was invalid (per-server resources, #722).

    The allocation (``config['cpu_millis']``, in millicores; 1000 = one core) is a
    *soft, rough* relative share, not a hard cap. It must be a positive integer
    (``bool`` rejected) within the accepted range: at least
    ``CPU_ALLOCATION_FLOOR_MILLIS`` (below it the server's main tick thread cannot
    make progress) and no more than ``CPU_ALLOCATION_CEILING_MILLIS`` (an absurd
    value is a typo, not an intent). The edge maps this to 422.
    """


class RetiredConfigKeyError(ServerError):
    """A create/update carried a config key retired by a later cutover (#1840).

    The FR-BAK-3 per-server backup cadence is retired into a first-class
    ``backup`` schedule (DATABASE.md Section 8): the legacy
    ``backup_interval_hours`` key no longer has any meaning, so a request still
    carrying it is rejected rather than silently written into ``server.properties``
    as a bogus override. The exception message carries the offending key. The edge
    maps this to 422.
    """


class ServerNotStoppedError(ServerError):
    """An operation requiring a fully stopped server ran against a live one.

    Config/name edits and deletion are allowed only while the server is at rest:
    ``desired_state == stopped`` and ``observed_state in {stopped, unknown}``
    (Section 6.9 spirit — avoid diverging from a live working set).
    """


class InvalidLifecycleTransitionError(ServerError):
    """A lifecycle op was requested against an incompatible desired state.

    Starting a server whose desired state is already ``running``, or
    stopping/restarting one whose desired state is ``stopped``, is a conflicting
    transition (FR-SRV-2). The edge maps this to 409.
    """


class LifecycleTransitionConflictError(ServerError):
    """A concurrent lifecycle transition lost a compare-and-set race.

    The in-memory transition check admitted the op, but the persisted
    compare-and-set (UPDATE ... WHERE desired_state = expected, plus any
    transition precondition) matched no row: another concurrent transition
    already moved the server out of the expected state. The use case aborts
    *before* dispatching or touching placement-load counts so a lost race causes
    no double placement/dispatch; the edge maps this to 409 ``transition_conflict``.
    """


class ServerBusyError(ServerError):
    """A gated operation could not acquire the per-server lifecycle lock in time.

    The at-rest-gated use cases and ``StartServer`` serialize on a per-server
    advisory lock (issue #827). When the lock is already held by another in-flight
    lifecycle operation for the same server, a waiter bounds its wait (issue #876):
    rather than block indefinitely — pinning a DB pool slot and risking process-wide
    pool starvation — it gives up after a short window and raises this. The
    condition is transient (the holder releases when its operation finishes), so the
    edge maps it to 409 ``server_busy`` and the caller can retry.
    """


class EulaNotAcceptedError(ServerError):
    """The server's eula.txt does not contain ``eula=true``.

    Starting without EULA acceptance would crash the Minecraft process
    immediately. The edge maps this to 409 ``eula_not_accepted`` so the UI
    can offer an inline acceptance dialog and retry with ``accept_eula=true``.
    """


class NoEligibleWorkerError(ServerError):
    """Placement found no Worker that can host the server (FR-WRK-3).

    No connected, non-draining Worker has free capacity. The edge maps this to
    a typed 409.
    """


class ServerNotRunningError(ServerError):
    """An RCON/console command targeted a server that is not observed running.

    Forwarding a console line is only meaningful for a live server
    (CONTROL_PLANE.md Section 7 ``INVALID_STATE``); the edge maps this to 409.
    """


class CommandDispatchError(ServerError):
    """A dispatched lifecycle/RCON command was refused by the Worker.

    The Worker returned a ``CommandResult`` failure (CONTROL_PLANE.md Section 7).
    For a start, the use case compensates the desired/assignment write before
    raising. The edge maps this to a typed 409.

    ``reason`` optionally names a sanitized failure category (e.g.
    ``"port_conflict"``, ``"image_missing"``, issue #225) the edge renders as the
    409 body reason instead of the generic ``command_failed``. It stays ``None``
    for ordinary dispatch failures. The raw Worker message is never the reason:
    it can leak Worker host paths, so it is logged, not returned.
    """

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class ServerFileNotFoundError(ServerError):
    """A file/version operation targeted a path or version that does not exist.

    Raised on the at-rest path (Storage ``NotFoundError``) and the running path
    (Worker ``SERVER_NOT_FOUND``). The edge maps this to 404, with the same
    no-existence-signal posture as a missing server.
    """


class InvalidFilePathError(ServerError):
    """A file path was rejected as traversal-unsafe (FR-FILE-4).

    Raised on the at-rest path (Storage ``PathTraversalError``) and the running
    path (Worker ``FILE_ACCESS_DENIED``): an absolute path, a ``..`` component, or
    a symlink escape. The edge maps this to 422; the rejection is explicit, never
    a silent clamp.

    ``reason`` names the 422 problem reason the edge renders (issue #548). It
    defaults to ``"invalid_path"`` (a genuine path-syntax rejection). The running
    path refines it from the Worker's :class:`FileAccessReason` so a non-path
    denial surfaces honestly: ``"is_a_directory"`` (read/edit of a directory),
    ``"not_a_directory"`` (list of a file), or ``"symlink_refused"`` (a refused
    symlink). The oversized case is not carried here — it is raised as
    :class:`FileTooLargeError` (413) instead.
    """

    def __init__(self, message: str = "", *, reason: str = "invalid_path") -> None:
        super().__init__(message)
        self.reason = reason


class InvalidVersionIdError(ServerError):
    """A version id was malformed (outside the retained-version id charset).

    Raised at the at-rest seam when a client-supplied ``version_id`` does not
    match the storage ``VersionId`` charset (``[a-zA-Z0-9_-]``): the rollback and
    version-preview routes carry the raw id straight to the seam, so a bad id is a
    client error, not an internal fault. The edge maps this to 422
    ``invalid_version_id`` (distinct from the path-syntax ``invalid_path``).
    """


class FileTooLargeError(ServerError):
    """An edit exceeded the file-size cap (Section 6.10 bounds).

    File access rides the control plane for small, interactive edits
    (ARCHITECTURE.md Section 7.2), so a write is bounded to a few MiB; an oversized
    edit is refused at the edge before dispatch. The edge maps this to 413.
    """


class FileAlreadyExistsError(ServerError):
    """A rename targeted a destination path that already exists (issue #259).

    Rename refuses to clobber an existing destination (file or directory): the
    caller must delete it first if that is the intent, so a typo cannot silently
    overwrite data. The edge maps this to 409.
    """


class ServerFilesUnsettledError(ServerError):
    """A file operation hit a server in a transitional state (Section 6.9).

    The state-branching policy routes a *stopped* server to Storage and a
    *running* server to its Worker; a server that is starting, stopping,
    restarting, crashed, or otherwise not settled in either resting state has no
    well-defined target. The edge maps this to 409 rather than guessing.
    """


class ContentDirProtectedError(ServerError):
    """A Files API operation targeted a path under the plugin content directory.

    The content directory (``mods/`` for Fabric/Forge, ``plugins/`` for Paper)
    is managed exclusively by the Plugin API; writes, deletes, renames, and
    uploads through the Files API are rejected to prevent DB/FS divergence
    (issue #1331). The edge maps this to 409 ``content_dir_protected``.
    """


class BackupNotFoundError(ServerError):
    """A backup operation targeted a backup that does not exist for the server.

    Raised by restore/delete when the backup id is unknown or belongs to a server
    outside the path community: reported as not-found so no cross-community
    existence signal leaks (FR-COMM-3), the same posture as a missing server. The
    edge maps this to 404.
    """


class InvalidRetentionPolicyError(ServerError):
    """A backup retention policy failed its validation rules (issue #1841).

    A policy is **either** keep-N (``keep_last >= 1``) **or** tiered
    (``daily`` / ``weekly`` / ``monthly`` each >= 0, at least one > 0) — never
    both, never neither. Raised on construction from API fields and on parsing
    a persisted JSON shape. The edge maps this to 422.
    """


class BackupUnsettledError(ServerError):
    """A create-backup hit a server in a transitional state (Section 6.9).

    The 6.9 policy archives a *stopped* server directly from Storage and a
    *running* server via save-all -> snapshot -> archive; a server that is
    starting, stopping, restarting, crashed, or otherwise not settled in either
    resting state has no well-defined source. The edge maps this to 409.
    """


class BackupCorruptError(ServerError):
    """A create-backup hit a structurally corrupt working set (issue #739).

    The authoritative-create integrity gate (issue #738) walks the working set for
    structurally corrupt ``.mca`` region files before archiving it; a crash during a
    chunk save can truncate a region (#703). Fail-closed: a known-corrupt world is
    never archived, so the create is refused and no archive is written. The seam
    translation of the storage ``IntegrityCheckError``; the edge maps it to 500
    (the data is corrupt on the server, not a client error) and audits the refusal.

    ``corrupt_count`` is the number of corrupt region files, carried through so the
    edge can log/audit *why* without the storage type leaking across the seam.
    """

    def __init__(self, identifier: str, *, corrupt_count: int) -> None:
        super().__init__(identifier)
        self.corrupt_count = corrupt_count


class BackupStorageUnavailableError(ServerError):
    """A backup operation failed because the storage backend was unavailable (#2270).

    The seam translation of the storage ``ObjectStoreUnavailableError``: the object
    store surfaced a transport failure or a backend service error (e.g. a SeaweedFS
    HTTP 500 ``InternalError`` on ``UploadPart``, the 2026-07-23 incident) that is not
    one of the modelled outcomes. Translating it at the servers/storage seam keeps the
    raw storage type from crossing back into the servers layer (the seam's documented
    contract). The condition is a transient backend fault, distinct from a corrupt
    working set (:class:`BackupCorruptError`); an unmapped ``ServerError`` surfaces as
    a generic 500 at the edge, which a future dedicated handler may refine to a 503.
    """


class InvalidBackupArchiveError(ServerError):
    """An uploaded backup archive was not a valid, traversal-safe ``tar.gz`` (#281).

    The upload validates the archive BEFORE storing it: it must open as a gzip tar
    and every member must be a traversal-safe relative path (no absolute paths, no
    ``..`` escapes, no devices/symlink/hardlink members). A body that does not open
    as a tar.gz, or that carries an unsafe member, is rejected here so a hostile
    archive never lands in the store. The edge maps this to 422.
    """


class InvalidExportMetadataError(ServerError):
    """A server-import archive carried a missing/malformed ``export_metadata.json``.

    The import (issue #274) reads the descriptor from the uploaded zip's root and
    validates its ``format`` version and required fields before creating anything.
    A non-zip body, a missing/unreadable metadata member, malformed JSON, an
    unsupported format version (legacy incompatibility is loud, not silent), or a
    missing field is rejected; the edge maps this to 422. The ``server_type`` /
    ``mc_version`` themselves are validated downstream by the shared create path
    (the version validator), which yields its own distinct 422 reasons.
    """


class InvalidGroupNameError(ServerError):
    """A player-group name failed its validation rules (e.g. blank, issue #276)."""


class InvalidGroupKindError(ServerError):
    """The group ``kind`` is outside the supported set (op / whitelist, #276).

    The edge maps this to 422.
    """


class InvalidPlayerError(ServerError):
    """A player entry was invalid (e.g. blank username, issue #276).

    The edge maps this to 422.
    """


class GroupNotFoundError(ServerError):
    """A group operation targeted a group that does not exist in the community.

    Raised when the group id is unknown or belongs to a *different* community
    (cross-community access): reported as not-found so no cross-community existence
    signal leaks (FR-COMM-3), the same posture as a missing server. The edge maps
    this to 404.
    """


class GroupNameAlreadyExistsError(ServerError):
    """Create/rename hit the per-community, per-kind group name uniqueness rule.

    A group name is unique within ``(community_id, kind)``. The edge maps this to
    409 (issue #276).
    """


class GroupAttachmentNotFoundError(ServerError):
    """A detach targeted a group/server pair that is not attached (issue #276).

    The edge maps this to 404.
    """


class InvalidSlugError(ServerError):
    """A slug failed DNS-label format or reserved-word check (issue #955).

    A slug must match ``^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`` and must not be
    a reserved operational hostname (``www``, ``api``, ``relay``, …). The edge maps
    this to 422.
    """


class SlugAlreadyTakenError(ServerError):
    """A slug rename hit the deployment-wide uniqueness constraint (issue #955).

    Slugs are unique across all servers (not community-scoped): a slug already held
    by another server is a conflict. The edge maps this to 409.
    """


class SlugExhaustedError(ServerError):
    """Auto-generation could not find a unique slug in the retry budget (issue #955).

    This is a transient capacity condition (extremely unlikely in practice); the
    edge maps it to 503 so the caller can retry without a client-side code change.
    """


class PluginNotFoundError(ServerError):
    """A plugin operation targeted a plugin that does not exist for the server.

    Raised when the plugin id is unknown or belongs to a different server;
    reported as not-found so no cross-server existence signal leaks. The edge
    maps this to 404.
    """


class UnsupportedPluginServerTypeError(ServerError):
    """The server type does not support plugin/mod content management.

    Vanilla has no managed content directory; the edge maps this to 422
    ``unsupported_server_type``.
    """


class InvalidModJarError(ServerError):
    """A jar's bytes could not be read as a bounded zip (issue #1307).

    Raised by the manifest parser when the bytes are not a readable zip, carry
    too many entries, or decompress past the size cap (a decompression bomb). A
    readable jar with no recognized manifest is *not* an error -- the parser
    returns empty metadata so the install still proceeds (the loader is known
    from the server type regardless of the manifest).
    """


class PluginAlreadyExistsError(ServerError):
    """A plugin install hit the per-server rel_path uniqueness constraint.

    The edge maps this to 409 ``plugin_already_exists``.
    """


class InvalidPluginSideError(ServerError):
    """A side override requested a value outside server/client/both (issue #1308).

    The edge maps this to 422 ``invalid_side``.
    """


class CatalogUnavailableError(ServerError):
    """External catalog API unreachable or errored.

    The edge maps this to 502 ``catalog_unavailable``.
    """


@contextmanager
def wrap_shape_errors(source_name: str) -> Iterator[None]:
    """Wrap parse-level exceptions from malformed upstream payloads.

    Catches ``AttributeError``, ``KeyError``, ``TypeError``, and ``IndexError``
    raised while destructuring a catalog response and re-raises them as
    :class:`CatalogUnavailableError`.  A ``CatalogUnavailableError`` already in
    flight is passed through unchanged.
    """

    try:
        yield
    except CatalogUnavailableError:
        raise
    except (AttributeError, KeyError, TypeError, IndexError) as exc:
        raise CatalogUnavailableError(f"unexpected response shape: {exc}") from exc


class CatalogProjectNotFoundError(ServerError):
    """Project/version not found on the catalog.

    The edge maps this to 404 ``catalog_project_not_found``.
    """


class CatalogChecksumMismatchError(ServerError):
    """Downloaded file SHA-512 doesn't match catalog's published hash.

    The edge maps this to 502 ``checksum_mismatch``.
    """


class InvalidResourcePackError(ServerError):
    """The uploaded zip is not a valid Minecraft resource pack.

    Raised when the zip cannot be normalized into a valid resource pack: not a
    zip, no ``pack.mcmeta``, invalid ``pack.mcmeta`` content, ambiguous
    structure, zip bomb, or path traversal. The edge maps this to 422
    ``invalid_resource_pack``.
    """


class ResourcePackNotFoundError(ServerError):
    """A resource pack operation targeted a pack that does not exist.

    Raised by get/delete when the pack id is unknown. The edge maps this to 404.
    """


class ResourcePackInUseError(ServerError):
    """A resource pack cannot be deleted because it is assigned to servers.

    Raised by delete when one or more servers still reference the pack. The
    caller must remove the assignments first. The edge maps this to 409.
    """


class InvalidScheduleError(ServerError):
    """A schedule violated a domain invariant (the general scheduler, epic #649).

    Base class for the schedule validation failures below; also raised directly
    for entity-state invariants (a disabled schedule carrying a ``next_run_at``).
    The edge maps the family to 422.
    """


class InvalidScheduleNameError(InvalidScheduleError):
    """A schedule name failed its validation rules (e.g. blank)."""


class InvalidScheduleCadenceError(InvalidScheduleError):
    """A cadence was not exactly one of cron / interval, or the value was invalid.

    A schedule fires on a cron expression XOR a fixed positive interval
    (DATABASE.md's ``ck_schedule_cadence_xor``); both, neither, a blank cron
    string, or a non-positive interval are rejected here.
    """


class InvalidCronExpressionError(InvalidScheduleError):
    """A cron expression failed the ``NextRunCalculator`` syntax validation.

    Raised by the calculator adapter (cron parsing lives outside the stdlib-only
    domain); distinct from :class:`InvalidScheduleCadenceError`, which covers
    the cron-XOR-interval shape, not cron syntax.
    """


class InvalidScheduleTimezoneError(InvalidScheduleError):
    """A schedule timezone is not a known IANA zone (zoneinfo-validated)."""


class InvalidSchedulePayloadError(InvalidScheduleError):
    """A schedule's payload does not fit its action (issue #1835).

    ``command`` requires a non-blank single-line command and no warning steps;
    ``stop`` / ``restart`` accept up to five ``{offset_minutes, message}``
    warning steps (offsets positive, distinct, at most 120 minutes) and no
    command; ``start`` / ``backup`` accept neither.
    """


class ScheduleNotFoundError(ServerError):
    """A schedule operation targeted a schedule that does not exist for the server.

    Raised by read/update/delete/runs when the schedule id is unknown or belongs
    to a *different* server (or a server outside the path community): reported as
    not-found so no cross-server/community existence signal leaks (FR-COMM-3), the
    same posture as a missing server. The edge maps this to 404.
    """


class ScheduleNameAlreadyExistsError(ServerError):
    """Create/rename hit the per-server schedule name uniqueness constraint.

    A schedule name is unique within a server (DATABASE.md's
    ``uq_schedule_server_id_name``). The edge maps this to 409.
    """
