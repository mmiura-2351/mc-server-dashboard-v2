"""Edge configuration loader (CONFIGURATION.md Sections 1-3).

Read **only** at the edge / wiring layer (ARCHITECTURE.md Section 2.1). Sources,
lowest to highest precedence: code defaults < TOML config file < ``MCD_API_``
environment variables. Secrets come from the environment and are masked in any
dump. A missing required key or an unknown key is a fatal startup error: the
loader raises rather than starting half-configured.

Only the keys needed by the current skeleton are modelled (CONFIGURATION.md
Section 5.1/5.5); the rest land with their features.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from mc_server_dashboard_api.identity.domain.password_policy import PRESETS

_MASK = "***"


def _blank_to_none(value: str | None) -> str | None:
    """Treat a blank value as missing for an optional secret.

    compose interpolates an unset/blank ``${VAR}`` to an EMPTY string rather than
    dropping the variable, so an optional secret sourced from the environment can
    arrive as ``""`` (or whitespace). Collapse that to ``None`` so the edge's
    existing ``is None`` fail-fast guards (app factory, Section 3) treat a blank
    value as missing instead of booting with an empty key/credential (#939).
    """

    if value is not None and not value.strip():
        return None
    return value


def _require_nonblank(value: str) -> str:
    """Reject a blank value for a required secret-bearing setting.

    The required setting has no default, so pydantic only enforces presence — an
    empty string from a blank ``${VAR}`` interpolation passes that check and boots
    a broken deployment. Fail fast at load instead (#939).
    """

    if not value.strip():
        raise ValueError("must not be blank")
    return value


# Minimum HS256 signing-key length: the key is the shared-secret entropy of the
# MAC, so it should be at least as long as the 256-bit (32-byte) digest
# (CONFIGURATION.md Section 5.3).
_HS256_MIN_KEY_BYTES = 32

# Floor for the reconciler's backoff-entry expiry slack (issue #353). The
# crash-loop damping added in #346 keeps a flapping server's backoff entry alive
# until ``now`` is past ``next_eligible_at`` by ``backoff_max_seconds`` of slack;
# a still-diverged server that briefly drops out of the reconcilable set (sitting
# in ``starting`` during a slow modded boot) must re-arrive and refresh its entry
# within that slack, or its failure count is silently reset and the boot-crash
# loop re-arms. The slack therefore has to comfortably exceed the longest
# plausible ``starting`` window. Ten minutes covers even a heavily-modded boot
# while staying far below the shipped 3600s default.
_RECONCILER_BACKOFF_EXPIRY_SLACK_FLOOR_SECONDS = 600


class _Section(BaseModel):
    # Reject unknown keys (fail-fast) and forbid mutation after load.
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerSettings(_Section):
    """HTTP + control-plane transport (CONFIGURATION.md Section 5.1)."""

    host: str = "0.0.0.0"
    # 0..65535: 0 is the conventional "bind an OS-assigned ephemeral port" value
    # (used by the gRPC server's ``add_insecure_port`` in tests), so it is allowed;
    # only a negative or above-65535 port is rejected.
    http_port: int = Field(default=8000, ge=0, le=65535)
    grpc_port: int = Field(default=50051, ge=0, le=65535)
    # Externally reachable base URL of the API's HTTP data plane, advertised to
    # Workers in the hydrate/snapshot transfer triggers (CONFIGURATION.md
    # Section 5.1, REQUIREMENTS.md Section 5.2). Declared optional so a process
    # that never dispatches a transfer (no lifecycle commands) need not supply
    # it; the lifecycle layer requires it when it builds a transfer URL.
    public_base_url: str | None = None


class ControlTlsSettings(_Section):
    """Control-channel TLS material for the gRPC listener (CONFIGURATION.md
    Section 5.1, REQUIREMENTS.md NFR-SEC-1).

    The control channel must be authenticated AND encrypted. ``cert_file`` and
    ``key_file`` are the server certificate / private key the gRPC listener
    presents; both must be set together to serve over TLS. ``insecure`` opts in
    to a plaintext listener for local/dev only — the API logs a ``WARN`` at
    startup. The required-unless-insecure rule (enforced by the app factory)
    mirrors the Worker's ``api.tls.*`` precedent (Section 6.1): with neither the
    cert/key pair nor ``insecure=true`` set, startup fails fast.

    ``client_ca_file`` is reserved for client-certificate (mTLS) verification
    and is **documented-deferred** in M1: the shared credential authenticates
    the Worker (NFR-SEC-1), so M1 ships server-side TLS only. The key exists so
    the config shape stays forward-compatible; it is currently unused.
    """

    cert_file: str | None = None
    key_file: str | None = None
    insecure: bool = False
    # Documented-deferred (M1): client-cert (mTLS) verification. Unused today.
    client_ca_file: str | None = None


class ControlSettings(_Section):
    """Control-plane (Worker channel) settings (CONFIGURATION.md Section 5.1).

    ``enabled`` gates whether the API hosts the control-plane gRPC server in
    this process; ``heartbeat_timeout_seconds`` is the liveness window past
    which a Worker missing heartbeats is marked offline (FR-WRK-2). The
    ``heartbeat_interval`` the API advertises in ``RegisterAck`` is derived from
    the timeout so a Worker normally beats several times before the window
    lapses.

    ``worker_credential`` is the shared secret a Worker presents to authenticate
    its stream (NFR-SEC-1); it is the API-side counterpart of the Worker's
    ``api.credential`` (CONFIGURATION.md Section 6.1). Like the token signing key
    it is declared optional here so a process that disables the control plane
    need not supply it; the app factory fails fast when the control plane is
    enabled without a credential (Section 3, fail-fast on a missing required
    secret).
    """

    enabled: bool = True
    # A zero/negative liveness window would mark every Worker offline immediately;
    # require a positive number of seconds.
    heartbeat_timeout_seconds: int = Field(default=30, gt=0)
    # Deadline for a dispatched ApiCommand to be answered by a CommandResult
    # (CONTROL_PLANE.md Section 4.2); a command unanswered within it is a typed
    # timeout the lifecycle layer treats as a failure. A zero/negative deadline
    # would fail every command immediately.
    command_timeout_seconds: int = Field(default=30, gt=0)
    # Separate, generous deadline for the HYDRATE phase of a start (issue #822):
    # a working-set pull for a large world routinely exceeds the general command
    # timeout, so the hydrate trigger gets its own budget rather than forcing
    # operators to inflate the global ``command_timeout_seconds`` (which governs
    # every other command, and widens the duplicate-start window — see the grace
    # invariant in app.py). A zero/negative deadline would fail every hydrate.
    hydrate_timeout_seconds: int = Field(default=600, gt=0)
    # Separate, generous deadline for the final SNAPSHOT a graceful stop captures
    # (issue #847): the stop holds the assignment until this snapshot settles, so the
    # dispatch must span a full working-set upload (minutes for a large world) — under
    # the general 30s ``command_timeout_seconds`` the dispatch would time out and the
    # assignment would be released while the upload is still in flight, reopening the
    # stop->re-place race the hold exists to close. Mirrors ``hydrate_timeout_seconds``
    # (#822/#868). A zero/negative deadline would fail every final snapshot.
    snapshot_timeout_seconds: int = Field(default=600, gt=0)
    worker_credential: str | None = None
    tls: ControlTlsSettings = Field(default_factory=ControlTlsSettings)

    # A blank ``${MCD_API_CONTROL__WORKER_CREDENTIAL}`` arrives as "" rather than
    # unset; collapse it to None so the app factory's fail-fast (control enabled
    # without a credential) treats a blank as missing (#939).
    _blank_credential = field_validator("worker_credential")(_blank_to_none)


class LogSettings(_Section):
    """Observability (CONFIGURATION.md Section 5.5)."""

    level: Literal["debug", "info", "warning", "error"] = "info"
    format: Literal["json", "text"] = "json"


class DatabaseSettings(_Section):
    """Persistence (CONFIGURATION.md Section 5.2). ``url`` is a secret."""

    url: str
    # Connection-pool sizing (issue #884). Defaults mirror SQLAlchemy's own
    # defaults (pool_size=5, max_overflow=10) so existing deployments see no
    # change until an operator explicitly tunes them.
    pool_size: int = Field(default=5, gt=0)
    max_overflow: int = Field(default=10, ge=0)

    # ``url`` is required and carries the DB password; a blank value (a blank
    # ``${MCD_API_DATABASE__URL}`` interpolation) passes the presence check but
    # boots an engine that cannot connect. Reject it at load (#939).
    _nonblank_url = field_validator("url")(_require_nonblank)


class StorageFsSettings(_Section):
    """Filesystem-backend settings (CONFIGURATION.md Section 5.2).

    ``root`` is the directory the ``fs`` adapter roots its tree at (STORAGE.md
    Section 2). Read when ``storage.backend`` is ``fs`` or ``remote-fs``; for
    ``remote-fs`` point it at the POSIX mount path (STORAGE.md Section 7.2).
    """

    root: str = "./data"


class StorageObjectSettings(_Section):
    """Object-storage-backend settings (CONFIGURATION.md Section 5.2).

    The S3-compatible endpoint/bucket and the access-key/secret-key credentials
    behind the ``object`` adapter (STORAGE.md Section 7.3). Only read when
    ``storage.backend = object``; all four are required in that case, enforced at
    the edge. ``access_key`` / ``secret_key`` are secrets sourced from the
    environment and masked in any dump (Section 5.2 marks them secret).
    """

    endpoint: str | None = None
    bucket: str | None = None
    access_key: str | None = None
    secret_key: str | None = None


class StorageSettings(_Section):
    """Storage adapter selection + tuning (CONFIGURATION.md Section 5.2).

    ``backend`` selects the :class:`Storage` Port adapter; ``fs`` is the M1 default.
    ``object`` binds the S3-compatible adapter (STORAGE.md Section 7.3); ``remote-fs``
    reuses the ``fs`` adapter over a POSIX mount (Section 7.2). Choosing a backend
    before its adapter lands, or without the keys it requires, fails fast at the
    edge. ``version_retention`` bounds per-file retained versions (STORAGE.md
    Section 5, the count-bounded retention knob).
    """

    backend: Literal["fs", "remote-fs", "object"] = "fs"
    fs: StorageFsSettings = Field(default_factory=StorageFsSettings)
    object: StorageObjectSettings = Field(default_factory=StorageObjectSettings)
    # 0 retains no prior versions (keep only the live file); a negative count is
    # meaningless.
    version_retention: int = Field(default=10, ge=0)


class SnapshotSettings(_Section):
    """Snapshot cadence (CONFIGURATION.md Section 5.4, FR-DATA-7).

    ``default_interval_seconds`` is the global periodic interval applied to every
    running server; a per-server override (stored on the ``Server`` config blob,
    DATABASE.md Section 7) replaces it. ``min_interval_seconds`` is the floor an
    override may not go below, guarding against snapshot thrash; the effective
    interval is clamped to at least this value.
    """

    default_interval_seconds: int = Field(default=3600, gt=0)
    min_interval_seconds: int = Field(default=300, gt=0)

    @model_validator(mode="after")
    def _enforce_floor_below_default(self) -> SnapshotSettings:
        # The default interval is itself an effective cadence and is clamped to
        # the floor; a floor above the default would force every server above
        # the configured default, which is contradictory. Reject at load
        # (CONFIGURATION.md Section 5.4, fail-fast). Equal is fine.
        if self.min_interval_seconds > self.default_interval_seconds:
            raise ValueError(
                "snapshot.min_interval_seconds must be <= "
                "snapshot.default_interval_seconds"
            )
        return self


class BackupSettings(_Section):
    """Scheduled-backup cadence (FR-BAK-3).

    The per-server schedule itself lives on the ``Server`` config blob as an
    interval in hours (DATABASE.md Section 8); this only tunes how often the
    background scheduler *wakes* to check which servers are due.
    ``schedule_tick_seconds`` is the loop resolution — coarse, since backup
    cadence is measured in hours; it defaults to five minutes.
    """

    schedule_tick_seconds: int = Field(default=300, gt=0)


class ReconcilerSettings(_Section):
    """Desired/observed divergence reconciler (issue #101).

    The reconciler periodically re-dispatches durable-but-unsent lifecycle intent
    (a start/stop committed before a crash, or a compensation-failure orphan) so a
    desired/observed divergence converges instead of lingering. It is gated on the
    control plane like the snapshot/backup loops — with no Worker channel there is
    nothing to re-dispatch.

    ``interval_seconds`` is the loop resolution: how often it wakes to scan for
    diverged servers. ``grace_seconds`` is how long a divergence must persist
    before it is acted on, giving the normal in-flight lifecycle path time to
    converge (a mid-launch start reports ``starting``, not a divergence) before the
    reconciler intervenes. It is dominated by the hydrate budget because a start
    re-dispatch is hydrate-then-start and must not race an in-flight original
    dispatch (#822/#847). ``held_start_grace_seconds`` is the SHORTER grace applied
    only to a ``redispatch_start`` whose assigned Worker is connected AND already
    holds a fresh-enough working set, so the start skips hydrate and is command-only
    (issue #999): the long hydrate-based grace is pure dead waiting there, and the
    cross-worker duplicate-live-instance race the long grace guards cannot occur on
    a re-dispatch to the same already-connected Worker (its double-start guard
    rejects a second live start). All other paths keep the full ``grace_seconds``.
    ``backoff_base_seconds`` / ``backoff_max_seconds`` bound the per-server
    exponential backoff that prevents a persistently failing server from being
    retried every tick.
    """

    interval_seconds: int = Field(default=60, gt=0)
    grace_seconds: int = Field(default=660, gt=0)
    held_start_grace_seconds: int = Field(default=90, gt=0)
    backoff_base_seconds: int = Field(default=30, gt=0)
    backoff_max_seconds: int = Field(default=3600, gt=0)

    @model_validator(mode="after")
    def _enforce_backoff_ordering(self) -> ReconcilerSettings:
        # The exponential backoff caps the per-server delay at backoff_max_seconds;
        # a max below the base would clamp the very first retry below its base,
        # making the cap meaningless. Reject the inverted range at load (fail-fast).
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError(
                "reconciler.backoff_max_seconds must be >= "
                "reconciler.backoff_base_seconds"
            )
        # backoff_max_seconds is also the slack past next_eligible_at that keeps a
        # flapping server's backoff entry alive while it briefly sits in starting
        # (#346). Too small a slack lets a still-diverged server in a slow modded
        # boot expire and reset its failure count, re-arming the crash-loop. Reject
        # a slack below the floor that a plausible boot window could outlast (#353).
        if self.backoff_max_seconds < _RECONCILER_BACKOFF_EXPIRY_SLACK_FLOOR_SECONDS:
            raise ValueError(
                "reconciler.backoff_max_seconds must be >= "
                f"{_RECONCILER_BACKOFF_EXPIRY_SLACK_FLOOR_SECONDS}: it doubles as "
                "the backoff-entry expiry slack that keeps crash-loop damping "
                "alive across a slow (modded) boot's starting window; a smaller "
                "slack lets a still-diverged server expire and reset its failure "
                "count, re-arming the boot-crash loop"
            )
        return self


class JarGcSettings(_Section):
    """Reference-counted JAR-pool garbage collection cadence (D4, issue #293).

    The GC reclaims pooled JARs no live server row references (a bounded DB scan
    diffed against the pool). It is gated on the control plane like the
    snapshot/backup/reconciler loops. ``interval_seconds`` is how often the loop
    wakes to sweep; a pool grows slowly (one entry per distinct resolved JAR), so
    a daily default is ample. A platform admin can also trigger a sweep on demand.
    """

    interval_seconds: int = Field(default=86400, gt=0)


class PortsSettings(_Section):
    """Game-port range for create-time auto-assignment (issue #243).

    The API tracks each server's Minecraft game port (``server.game_port``,
    DATABASE.md Section 7) and assigns the lowest free in-range port at create,
    unique deployment-wide. ``range_start`` / ``range_end`` bound the assignable
    range (inclusive); the default ``25565..25664`` is a hundred-port window from
    the conventional Minecraft port. Both must be a valid TCP port (1..65535) and
    ``range_start <= range_end`` (an inverted range admits no assignable port).
    """

    range_start: int = Field(default=25565, gt=0, le=65535)
    range_end: int = Field(default=25664, gt=0, le=65535)

    @model_validator(mode="after")
    def _enforce_start_below_end(self) -> PortsSettings:
        # An inverted range (start > end) admits no port to assign; reject it at
        # load (fail-fast). Equal is fine (a single assignable port).
        if self.range_start > self.range_end:
            raise ValueError("ports.range_start must be <= ports.range_end")
        return self


class RelaySettings(_Section):
    """Game-ingress relay control surface (CONFIGURATION.md, RELAY.md Section 12).

    ``enabled`` is the master switch (default off, RELAY.md Section 9): it gates
    serving ``RelayService`` on the gRPC listener, exposing ``join_hostname`` on
    server responses, and (issue #957) the session prune loop. ``credential`` is
    the shared secret the relay presents (a separate credential from the Worker's
    so they rotate independently, RELAY.md Section 6); ``base_domain`` builds
    ``<slug>.<base_domain>`` for ``join_hostname`` and is returned to the relay on
    Register. Both are required when ``enabled`` is true, enforced at the edge
    (app factory) like the Worker credential — declared optional here so a
    deployment that leaves the relay off need not supply them.
    ``session_retention_days`` is the ``game_session`` prune window consumed by
    issue #957; it is parsed and validated here only.
    """

    enabled: bool = False
    credential: str | None = None
    base_domain: str | None = None
    # The prune window for game_session rows (RELAY.md Section 8); consumed by
    # issue #957. A zero/negative window is meaningless (it would prune every row
    # or none); require a positive number of days.
    session_retention_days: int = Field(default=90, gt=0)

    # A blank ``${MCD_API_RELAY__CREDENTIAL}`` arrives as "" rather than unset;
    # collapse it to None so the app factory's fail-fast (relay enabled without a
    # credential) treats a blank as missing (#943).
    _blank_credential = field_validator("credential")(_blank_to_none)


class WebuiSettings(_Section):
    """Built Web UI static-serving (CONFIGURATION.md Section 5.10, WEBUI_SPEC 7.7).

    ``dist_dir`` points at the built SPA directory (``webui/dist``). When set, the
    API mounts it as same-origin static files with an SPA fallback, so deep links
    and reloads resolve to ``index.html`` while API routes keep precedence. When
    unset (the default), nothing is mounted — dev (Vite proxy) and tests are
    unaffected. The path must be an existing directory containing an
    ``index.html`` when set; otherwise startup fails fast (Section 3).
    """

    dist_dir: str | None = None


class PasswordSettings(_Section):
    """Password hashing + policy (CONFIGURATION.md Sections 5.3 and 7.1).

    ``hash`` selects the :class:`PasswordHasher` adapter (Section 4). ``policy``
    selects the password-strength preset enforced at registration and password
    change (SECURITY.md Section 1); the preset fixes which strength rules fire.
    ``max_length`` stays an independent DoS guard. The default preset is
    ``middle`` (issue #536): the historical fixed posture is the ``high`` preset,
    opt-in for operators who want it.
    """

    hash: Literal["argon2", "bcrypt"] = "argon2"
    policy: Literal["low", "middle", "high"] = "middle"
    max_length: int = Field(default=128, gt=0)

    @model_validator(mode="after")
    def _enforce_preset_min_below_max(self) -> PasswordSettings:
        # A preset minimum length above the configured maximum admits no valid
        # password. Each preset ships a small min (<= 12), so this only fires
        # when an operator drives max_length unusually low; reject at load
        # (SECURITY.md Section 1, fail-fast). Equal is fine.
        if PRESETS[self.policy].min_length > self.max_length:
            raise ValueError(
                f"auth.password.policy={self.policy} requires "
                f"min_length {PRESETS[self.policy].min_length} <= max_length "
                f"{self.max_length}"
            )
        return self


class TokenSettings(_Section):
    """Token issuing/verification (CONFIGURATION.md Section 5.3).

    Parameters of the single ``TokenService`` JWT adapter (Section 4 note):
    ``algorithm`` is HS256 by default (RS256 supported by supplying the matching
    key); ``signing_key`` is the secret used to sign access tokens. The key is
    declared optional here so the loader does not force it on processes that
    mount no auth endpoints; the app factory fails fast when it mounts the auth
    routers without a key (Section 3, fail-fast on a missing required secret).
    """

    # Constrained to the supported algorithms so a miscased/typo value fails at
    # load rather than slipping past the HS256 key-length floor below and only
    # failing at first JWT use (CONFIGURATION.md Section 5.3).
    algorithm: Literal["HS256", "RS256"] = "HS256"
    signing_key: str | None = None
    access_ttl_seconds: int = Field(default=900, gt=0)

    # A blank ``${MCD_API_AUTH__TOKEN__SIGNING_KEY}`` arrives as "" rather than
    # unset; collapse it to None so the app factory's fail-fast (signing key
    # required to mount auth) treats a blank as missing rather than booting with
    # an empty key — an empty RS256 key otherwise slips past every guard (#939).
    _blank_signing_key = field_validator("signing_key")(_blank_to_none)
    refresh_ttl_seconds: int = Field(default=1209600, gt=0)
    # Grace window after a refresh token is rotated within which re-presenting the
    # predecessor is treated as a legitimate concurrent refresh (a fresh pair is
    # issued, the token family is kept) rather than theft (issue #369). Outside the
    # window the reuse still revokes the whole family. Must be positive.
    refresh_reuse_grace_seconds: int = Field(default=60, gt=0)
    # Refresh-token httpOnly-cookie transport for the Web UI session (issue #363).
    # The cookie name and the Secure flag are operator-configurable; the cookie's
    # Max-Age tracks ``refresh_ttl_seconds`` and its Path/SameSite are fixed in the
    # auth router (SameSite=Strict; Path=/auth). ``refresh_cookie_secure`` defaults
    # to True (HTTPS only); turn it off for plain-HTTP localhost dev so the browser
    # stores the cookie.
    refresh_cookie_name: str = Field(default="mcd_refresh", min_length=1)
    refresh_cookie_secure: bool = True

    @model_validator(mode="after")
    def _enforce_hs256_key_length(self) -> TokenSettings:
        # For the symmetric HS256 the signing key is shared-secret entropy; a key
        # shorter than the 256-bit (32-byte) digest weakens the MAC. Reject a
        # too-short key at load (CONFIGURATION.md Section 5.3, fail-fast). RS256
        # keys are asymmetric PEM material, not raw entropy, so this floor does
        # not apply to them.
        if (
            self.algorithm == "HS256"
            and self.signing_key is not None
            and len(self.signing_key.encode("utf-8")) < _HS256_MIN_KEY_BYTES
        ):
            raise ValueError(
                "auth.token.signing_key must be at least "
                f"{_HS256_MIN_KEY_BYTES} bytes for HS256"
            )
        return self

    @model_validator(mode="after")
    def _enforce_access_below_refresh(self) -> TokenSettings:
        # The refresh token exists to outlive the short-lived access token; an
        # access TTL >= the refresh TTL means the refresh expires no later than
        # the access token, defeating the refresh mechanism. Strict ``<``:
        # equal lifetimes are just as nonsensical. Reject at load
        # (CONFIGURATION.md Section 5.3, fail-fast).
        if self.access_ttl_seconds >= self.refresh_ttl_seconds:
            raise ValueError(
                "auth.token.access_ttl_seconds must be < auth.token.refresh_ttl_seconds"
            )
        return self


class BruteForceSettings(_Section):
    """Brute-force protection (CONFIGURATION.md Section 7.2).

    Per-username and per-IP failure thresholds over sliding windows, lockout with
    exponential back-off, and the artificial failure delay against timing-based
    enumeration (SECURITY.md Section 2). Defaults are the proven legacy baseline.
    """

    enabled: bool = True
    # Thresholds must be >= 1 (a 0 threshold would lock out on the first attempt);
    # windows and lockout durations must be > 0 (a 0 window never accumulates, so
    # a threshold never trips and protection is silently disabled).
    username_threshold: int = Field(default=5, ge=1)
    username_window_seconds: int = Field(default=900, gt=0)
    ip_threshold: int = Field(default=20, ge=1)
    ip_window_seconds: int = Field(default=300, gt=0)
    lockout_base_seconds: int = Field(default=900, gt=0)
    lockout_max_seconds: int = Field(default=86400, gt=0)
    # The artificial failure delay against timing enumeration (SECURITY.md
    # Section 2). >= 0: 0 explicitly disables the delay; a negative value is
    # meaningless. Disabling it forgoes the timing-uniformity guarantee, so it is
    # an explicit operator choice rather than a silent default.
    delay_ms: int = Field(default=200, ge=0)
    # How often the background loop prunes ``login_attempt`` rows older than the
    # longest sliding window, independent of login events (SECURITY.md Section 3).
    # A failures-only attack never triggers the on-success prune, so this keeps
    # the append-only table bounded. Defaults to one hour.
    prune_interval_seconds: int = Field(default=3600, gt=0)

    @model_validator(mode="after")
    def _enforce_lockout_base_below_max(self) -> BruteForceSettings:
        # The lockout doubles from the base on each repeat and is capped at the
        # max (SECURITY.md Section 2); a base above the cap means the very first
        # lockout already exceeds its own ceiling, which is contradictory.
        # Reject at load (fail-fast). Equal is fine (a fixed-duration lockout).
        if self.lockout_base_seconds > self.lockout_max_seconds:
            raise ValueError(
                "auth.brute_force.lockout_base_seconds must be <= "
                "auth.brute_force.lockout_max_seconds"
            )
        return self


class RegistrationSettings(_Section):
    """Open-registration abuse controls (CONFIGURATION.md Section 7.4).

    ``POST /users`` is unauthenticated open registration (FR-AUTH-1); these knobs
    cover the abuse surface that authentication's FR-AUTH-4 hardening does not
    (issue #362). ``open`` is the master switch: turning it off makes a private,
    admin-provisioned deployment reject self-registration while the admin surface
    keeps creating accounts. The per-IP cap reuses FR-AUTH-4's
    ``login_attempt``-backed sliding window and the same trusted-proxy client-IP
    resolution. Defaults are consistent with the per-IP *login* throttle
    (Section 7.2): no proven legacy registration baseline exists, so the login
    per-IP knobs are the reference (issue #362).
    """

    open: bool = True
    ip_limit_enabled: bool = True
    # Registrations per source IP before throttling. Must be at least 1 (a 0
    # threshold would block the first attempt), mirroring the brute-force
    # thresholds.
    ip_threshold: int = Field(default=5, ge=1)
    # Sliding window for the per-IP registration count. Must be positive (a 0
    # window never accumulates, silently disabling the cap). An hour is far wider
    # than the 5-minute login window: registration is a rare, deliberate action,
    # so a long window catches a slow flood.
    ip_window_seconds: int = Field(default=3600, gt=0)


class ProxySettings(_Section):
    """Reverse-proxy trust (CONFIGURATION.md Section 7.3).

    Forwarded client IPs are honored only from explicitly trusted proxy peers
    (SECURITY.md Section 4); the per-IP brute-force counter depends on a
    trustworthy client IP.
    """

    trust_forwarded_headers: bool = False
    trusted_proxies: tuple[str, ...] = ()


class AuthSettings(_Section):
    """Authentication configuration (CONFIGURATION.md Section 5.3 / 7).

    The password, token, brute-force, and proxy-trust sub-groups are modelled
    here.
    """

    password: PasswordSettings = Field(default_factory=PasswordSettings)
    token: TokenSettings = Field(default_factory=TokenSettings)
    brute_force: BruteForceSettings = Field(default_factory=BruteForceSettings)
    registration: RegistrationSettings = Field(default_factory=RegistrationSettings)
    proxy: ProxySettings = Field(default_factory=ProxySettings)


class Settings(BaseSettings):
    """The fully-resolved configuration injected into the wiring layer."""

    # Known, deliberate asymmetry: ``extra="forbid"`` fails fast on unknown TOML
    # keys, but unknown ``MCD_API_*`` env vars are silently ignored — env sources
    # in pydantic-settings only feed declared fields, so stray env vars never
    # reach this check. Accepted: the environment is shared and may legitimately
    # carry unrelated ``MCD_API_*``-prefixed names.
    model_config = SettingsConfigDict(
        env_prefix="MCD_API_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
    )

    server: ServerSettings = Field(default_factory=ServerSettings)
    control: ControlSettings = Field(default_factory=ControlSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    database: DatabaseSettings
    storage: StorageSettings = Field(default_factory=StorageSettings)
    snapshot: SnapshotSettings = Field(default_factory=SnapshotSettings)
    backup: BackupSettings = Field(default_factory=BackupSettings)
    reconciler: ReconcilerSettings = Field(default_factory=ReconcilerSettings)
    jar_gc: JarGcSettings = Field(default_factory=JarGcSettings)
    ports: PortsSettings = Field(default_factory=PortsSettings)
    webui: WebuiSettings = Field(default_factory=WebuiSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    relay: RelaySettings = Field(default_factory=RelaySettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Reorder so environment outranks the explicitly-passed file values
        # (init kwargs): env > TOML file > code defaults (CONFIGURATION.md
        # Section 2). dotenv / file-secret sources are unused by this loader.
        return (env_settings, init_settings)

    def masked_dump(self) -> dict[str, Any]:
        """Config snapshot safe to log: secret values replaced with ``***``."""

        auth = self.auth.model_dump()
        # The token signing key is a secret (CONFIGURATION.md Section 5.3); mask
        # it whenever present. ``None`` (no key configured) is not a secret.
        if auth["token"]["signing_key"] is not None:
            auth["token"]["signing_key"] = _MASK
        control = self.control.model_dump()
        # The Worker credential is a secret (CONFIGURATION.md Section 5.1); mask
        # it whenever present. ``None`` (control plane disabled) is not a secret.
        if control["worker_credential"] is not None:
            control["worker_credential"] = _MASK
        storage = self.storage.model_dump()
        # The object-store access/secret keys are secrets (CONFIGURATION.md
        # Section 5.2); mask each whenever present. ``None`` (object backend
        # unused) is not a secret.
        for secret_key in ("access_key", "secret_key"):
            if storage["object"][secret_key] is not None:
                storage["object"][secret_key] = _MASK
        relay = self.relay.model_dump()
        # The relay credential is a secret (RELAY.md Section 6); mask it whenever
        # present. ``None`` (relay disabled) is not a secret.
        if relay["credential"] is not None:
            relay["credential"] = _MASK
        return {
            "server": self.server.model_dump(),
            "control": control,
            "log": self.log.model_dump(),
            "database": {"url": _MASK},
            "storage": storage,
            "snapshot": self.snapshot.model_dump(),
            "backup": self.backup.model_dump(),
            "reconciler": self.reconciler.model_dump(),
            "ports": self.ports.model_dump(),
            "webui": self.webui.model_dump(),
            "auth": auth,
            "relay": relay,
        }


def _read_toml(config_file: Path) -> dict[str, Any]:
    with config_file.open("rb") as handle:
        return tomllib.load(handle)


def load_settings(config_file: Path | None) -> Settings:
    """Load and validate settings with defaults < file < env precedence.

    Raises ``ValueError`` (pydantic ``ValidationError``) on a missing required
    key or an unknown key, so the caller fails fast at boot (CONFIGURATION.md
    Section 2).
    """

    file_values = _read_toml(config_file) if config_file is not None else {}
    return Settings(**file_values)
