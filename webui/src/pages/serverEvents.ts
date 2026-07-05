/**
 * Per-server events WebSocket client (WEBUI_SPEC.md 6.4, 6.5, 7.2).
 *
 * A thin wrapper over the shared {@link EventsSocketClient} core
 * (`eventsSocket.ts`) for one open server-detail page's events stream
 * (`WS /communities/{cid}/servers/{sid}/events?streams=…`). The core owns the
 * socket lifecycle (connect, backoff reconnect, reconnect-on-rotate, teardown);
 * this module builds the stream-subscription URL and parses the API's typed
 * frames (`_frame` in events.py: `{stream, ts, payload}`, no `server_id` on the
 * per-server stream) into a discriminated {@link ServerFrame}.
 *
 * One connection per open detail page, shared by all tabs (Overview, Console):
 * the caller subscribes to all three streams once and routes parsed frames to
 * each tab's consumer (WEBUI_SPEC.md 7.2).
 */

import { EventsSocketClient, wsOrigin } from "./eventsSocket.ts";

/** The subscribable streams (the GAP marker is always delivered, never asked). */
export type Stream = "status" | "log" | "metrics";

/** A parsed status frame: the latest observed state + optional crash detail. */
export interface StatusFrame {
  kind: "status";
  state: string;
  detail: string;
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

/** A gap marker: the client fell behind and missed events (best-effort). */
export interface GapFrame {
  kind: "gap";
}

export type ServerFrame = StatusFrame | LogFrame | MetricsFrame | GapFrame;

export interface ServerEventsCallbacks {
  /** A parsed frame from any subscribed stream (or the GAP marker). */
  onFrame: (frame: ServerFrame) => void;
  /** The connection opened (clear degraded). */
  onOpen: () => void;
  /** The connection went down and a reconnect is pending (enter degraded). */
  onDown: () => void;
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
    const { state, detail } = payload as { state?: unknown; detail?: unknown };
    if (typeof state !== "string") {
      return null;
    }
    return {
      kind: "status",
      state,
      detail: typeof detail === "string" ? detail : "",
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
