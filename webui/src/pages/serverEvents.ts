/**
 * Per-server events WebSocket client (WEBUI_SPEC.md 6.4, 6.5, 7.2).
 *
 * A thin wrapper over the shared {@link EventsSocketClient} core
 * (`eventsSocket.ts`) for one open server-detail page's events stream
 * (`WS /communities/{cid}/servers/{sid}/events?streams=…`). The core owns the
 * socket lifecycle (connect, backoff reconnect, reconnect-on-rotate, teardown);
 * this module builds the stream-subscription URL and parses the API's typed
 * frames (`_frame` in events.py: `{stream, ts, payload}`, no `server_id` on the
 * per-server stream) into a discriminated {@link ServerFrame}, including the
 * status `snapshot` the API sends on subscribe and after every gap that dropped
 * status frames.
 *
 * One connection per open detail page, shared by all tabs (Overview, Console):
 * the caller subscribes to all three streams once and routes parsed frames to
 * each tab's consumer (WEBUI_SPEC.md 7.2).
 */

import { api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import { EventsSocketClient, wsOrigin } from "./eventsSocket.ts";

/** The subscribable streams (the GAP marker is always delivered, never asked). */
export type Stream = "status" | "log" | "metrics";

/**
 * A parsed status frame. It sets the server state fields it carries: an
 * observed transition carries `state` + optional crash detail, a change of the
 * server's intent carries `desiredState` (#3428), and a placement or release
 * carries `assignedWorkerId`, `null` once released (#3818). `reason` names a
 * crash the Worker classified (e.g. `forge_install_failed`, issue #1093); it is
 * empty for an unclassified crash and for every other state.
 */
export interface StatusFrame {
  kind: "status";
  state?: string;
  desiredState?: string;
  assignedWorkerId?: string | null;
  detail: string;
  reason: string;
}

/** A parsed log line: the text and which std stream it came from. */
export interface LogFrame {
  kind: "log";
  line: string;
  stream: "stdout" | "stderr";
}

/** A parsed metrics sample (cpu in milli-cores, memory in bytes, players). */
export interface MetricsFrame {
  kind: "metrics";
  cpuMillis: number;
  memoryBytes: number;
  playerCount: number;
}

/**
 * The last crash the API stores with a server's observed state (#3281): the
 * reason the Worker classified it with and the Worker's detail, each `null`
 * when there is none. Both are `null` unless the server reads `crashed`.
 */
export interface Crash {
  reason: string | null;
  detail: string | null;
}

/**
 * The status snapshot the API sends on subscribe and after every gap that
 * dropped status frames (#1795):
 * the server's persisted observed state and, when the API reports them, its
 * desired state (#3428), assigned Worker (#3818), last crash and whether the
 * reconciler leaves that crash alone (#3281). The live `detail` of any other
 * state is not stored, so it has no counterpart.
 */
export interface SnapshotFrame {
  kind: "snapshot";
  state: string;
  desiredState?: string;
  assignedWorkerId?: string | null;
  crash?: Crash;
  crashNotRestarted?: boolean;
}

/** A gap marker: the client fell behind and missed events (best-effort). */
export interface GapFrame {
  kind: "gap";
}

export type ServerFrame =
  | StatusFrame
  | SnapshotFrame
  | LogFrame
  | MetricsFrame
  | GapFrame;

export interface ServerEventsCallbacks {
  /** A parsed frame from any subscribed stream (or the GAP marker). */
  onFrame: (frame: ServerFrame) => void;
  /** The connection opened (clear degraded). */
  onOpen: () => void;
  /** The connection went down and a reconnect is pending (enter degraded). */
  onDown: () => void;
  /** The user lost read access to the server, or it is gone; the socket stopped. */
  onAccessLost: () => void;
}

/**
 * Build the `wss?://…/api/communities/{cid}/servers/{sid}/events?streams=…`
 * URL. `streams` is the comma list the API's `_parse_streams` expects.
 */
export function serverEventsUrl(
  communityId: string,
  serverId: string,
  streams: readonly Stream[],
): string {
  const path = `/api/communities/${encodeURIComponent(communityId)}/servers/${encodeURIComponent(serverId)}/events`;
  const query = `?streams=${encodeURIComponent(streams.join(","))}`;
  return `${wsOrigin()}${path}${query}`;
}

/**
 * The `assigned_worker_id` a status or snapshot payload carries: the Worker's
 * id, `null` for none, or `undefined` when the payload does not carry it (or
 * carries something that is neither).
 */
export function assignmentOf(payload: object): string | null | undefined {
  if (!("assigned_worker_id" in payload)) {
    return undefined;
  }
  const { assigned_worker_id } = payload;
  return assigned_worker_id === null || typeof assigned_worker_id === "string"
    ? assigned_worker_id
    : undefined;
}

/**
 * The last crash a snapshot payload carries, or `undefined` when it does not
 * carry both fields as a string or `null`.
 */
function crashOf(payload: object): Crash | undefined {
  if (!("crash_reason" in payload && "crash_detail" in payload)) {
    return undefined;
  }
  const { crash_reason: reason, crash_detail: detail } = payload;
  const valid = (value: unknown) => value === null || typeof value === "string";
  return valid(reason) && valid(detail)
    ? { reason: reason as string | null, detail: detail as string | null }
    : undefined;
}

/**
 * Parse a per-server wire frame into a {@link ServerFrame}, or null when it is
 * malformed or names a stream this client does not surface. The per-server
 * frame carries no `server_id` (one socket per server, so it is implied).
 */
export function parseServerFrame(raw: string): ServerFrame | null {
  let frame: unknown;
  try {
    frame = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof frame !== "object" || frame === null) {
    return null;
  }
  const { stream, payload } = frame as { stream?: unknown; payload?: unknown };
  if (stream === "gap") {
    return { kind: "gap" };
  }
  if (typeof payload !== "object" || payload === null) {
    return null;
  }
  if (stream === "status") {
    const { state, desired_state, detail, reason } = payload as {
      state?: unknown;
      desired_state?: unknown;
      detail?: unknown;
      reason?: unknown;
    };
    const assignedWorkerId = assignmentOf(payload);
    if (
      typeof state !== "string" &&
      typeof desired_state !== "string" &&
      assignedWorkerId === undefined
    ) {
      return null;
    }
    return {
      kind: "status",
      ...(typeof state === "string" && { state }),
      ...(typeof desired_state === "string" && { desiredState: desired_state }),
      ...(assignedWorkerId !== undefined && { assignedWorkerId }),
      detail: typeof detail === "string" ? detail : "",
      reason: typeof reason === "string" ? reason : "",
    };
  }
  if (stream === "snapshot") {
    const { state, desired_state } = payload as {
      state?: unknown;
      desired_state?: unknown;
    };
    if (typeof state !== "string") {
      return null;
    }
    const assignedWorkerId = assignmentOf(payload);
    const crash = crashOf(payload);
    const { crash_not_restarted } = payload as {
      crash_not_restarted?: unknown;
    };
    return {
      kind: "snapshot",
      state,
      ...(typeof desired_state === "string" && { desiredState: desired_state }),
      ...(assignedWorkerId !== undefined && { assignedWorkerId }),
      ...(crash !== undefined && { crash }),
      ...(typeof crash_not_restarted === "boolean" && {
        crashNotRestarted: crash_not_restarted,
      }),
    };
  }
  if (stream === "log") {
    const { line, stream: src } = payload as {
      line?: unknown;
      stream?: unknown;
    };
    if (typeof line !== "string") {
      return null;
    }
    return {
      kind: "log",
      line,
      stream: src === "stderr" ? "stderr" : "stdout",
    };
  }
  if (stream === "metrics") {
    const { cpu_millis, memory_bytes, player_count } = payload as {
      cpu_millis?: unknown;
      memory_bytes?: unknown;
      player_count?: unknown;
    };
    if (
      typeof cpu_millis !== "number" ||
      typeof memory_bytes !== "number" ||
      typeof player_count !== "number"
    ) {
      return null;
    }
    return {
      kind: "metrics",
      cpuMillis: cpu_millis,
      memoryBytes: memory_bytes,
      playerCount: player_count,
    };
  }
  return null;
}

/** All three subscribable streams, the page-wide subscription (one socket). */
export const ALL_STREAMS: readonly Stream[] = ["status", "log", "metrics"];

/**
 * Owns one server events socket and its reconnect loop. Construct it, call
 * {@link start}, and {@link close} it on page leave. Not for reuse across
 * servers — make a new one per open detail page.
 */
export class ServerEventsClient {
  private readonly socket: EventsSocketClient;

  constructor(
    communityId: string,
    serverId: string,
    callbacks: ServerEventsCallbacks,
    random: () => number = Math.random,
  ) {
    this.socket = new EventsSocketClient(
      () => serverEventsUrl(communityId, serverId, ALL_STREAMS),
      {
        onMessage: (raw) => {
          const frame = parseServerFrame(raw);
          if (frame !== null) {
            callbacks.onFrame(frame);
          }
        },
        onOpen: callbacks.onOpen,
        onDown: callbacks.onDown,
        onAccessLost: callbacks.onAccessLost,
        // The server read: the same two-layer gate as the socket (membership,
        // `server:read` on this server, the server in this community), so
        // its success admits the user and its 403 / 404 refuses them.
        probeAccess: () =>
          api
            .get(
              apiPath("/api/communities/{community_id}/servers/{server_id}", {
                community_id: communityId,
                server_id: serverId,
              }),
            )
            .then(() => true),
      },
      random,
    );
  }

  /** Open the socket and arm the reconnect loop. Idempotent per instance. */
  start(): void {
    this.socket.start();
  }

  /** Tear down: stop reconnecting, drop rotation hook, close the socket. */
  close(): void {
    this.socket.close();
  }
}
