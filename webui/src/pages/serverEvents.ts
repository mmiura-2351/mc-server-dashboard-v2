/** Parse one server's frames and share the socket across detail tabs; EventsSocketClient owns connection mechanics. */

import { EventsSocketClient, wsOrigin } from "./eventsSocket.ts";

export type Stream = "status" | "log" | "metrics";

/** A classified crash can carry reason/detail; unclassified crashes and other states may omit them. */
export interface StatusFrame {
  kind: "status";
  state: string;
  detail: string;
  reason: string;
}

export interface LogFrame {
  kind: "log";
  line: string;
  stream: "stdout" | "stderr";
}

export interface MetricsFrame {
  kind: "metrics";
  cpuMillis: number;
  memoryBytes: number;
  playerCount: number;
}

/** Subscribe and gap snapshots carry persisted state only; crash detail and reason exist only in live transitions. */
export interface SnapshotFrame {
  kind: "snapshot";
  state: string;
}

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
  onFrame: (frame: ServerFrame) => void;
  onOpen: () => void;
  onDown: () => void;
}

export function serverEventsUrl(
  communityId: string,
  serverId: string,
  streams: readonly Stream[],
): string {
  const path = `/api/communities/${encodeURIComponent(communityId)}/servers/${encodeURIComponent(serverId)}/events`;
  const query = `?streams=${encodeURIComponent(streams.join(","))}`;
  return `${wsOrigin()}${path}${query}`;
}

/** Per-server frames omit server_id because the socket target identifies the server; reject malformed or unknown frames. */
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
    const { state, detail, reason } = payload as {
      state?: unknown;
      detail?: unknown;
      reason?: unknown;
    };
    if (typeof state !== "string") {
      return null;
    }
    return {
      kind: "status",
      state,
      detail: typeof detail === "string" ? detail : "",
      reason: typeof reason === "string" ? reason : "",
    };
  }
  if (stream === "snapshot") {
    const { state } = payload as { state?: unknown };
    return typeof state === "string" ? { kind: "snapshot", state } : null;
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

export const ALL_STREAMS: readonly Stream[] = ["status", "log", "metrics"];

/** Use a new client per server and close it when leaving the detail page. */
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

  start(): void {
    this.socket.start();
  }

  close(): void {
    this.socket.close();
  }
}
