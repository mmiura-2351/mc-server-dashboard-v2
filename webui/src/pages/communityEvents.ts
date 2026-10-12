/** Parse community status, notification, snapshot, and gap frames; EventsSocketClient owns socket lifecycle. */

import {
  backoffDelayMs,
  EventsSocketClient,
  wsOrigin,
} from "./eventsSocket.ts";

export { backoffDelayMs };

export interface StatusEvent {
  serverId: string;
  state: string;
}

/** Notifications use {kind, title, detail}; a null server_id denotes a community-wide notice. */
export interface NotificationEvent {
  serverId: string | null;
  notificationKind: string;
  title: string;
  detail: string;
}

export type CommunityFrame =
  | ({ kind: "status" } & StatusEvent)
  | ({ kind: "notification" } & NotificationEvent)
  | { kind: "snapshot"; servers: StatusEvent[] }
  | { kind: "gap" };

export interface CommunityEventsCallbacks {
  onStatus: (event: StatusEvent) => void;
  onSnapshot: (servers: StatusEvent[]) => void;
  onNotification: (event: NotificationEvent) => void;
  /** Slow-client overflow dropped frames for an unknown set of servers. */
  onGap: () => void;
  onOpen: () => void;
  onDown: () => void;
}

function eventsUrl(communityId: string): string {
  return `${wsOrigin()}/api/communities/${encodeURIComponent(communityId)}/events`;
}

/** Reject malformed or unknown frames; a server-agnostic gap requires reconciling the whole list. */
export function parseCommunityFrame(raw: string): CommunityFrame | null {
  let frame: unknown;
  try {
    frame = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof frame !== "object" || frame === null) {
    return null;
  }
  const { stream, server_id, payload } = frame as {
    stream?: unknown;
    server_id?: unknown;
    payload?: unknown;
  };
  if (stream === "gap") {
    return { kind: "gap" };
  }
  if (stream === "notification") {
    return parseNotification(server_id, payload);
  }
  if (stream === "snapshot") {
    return parseSnapshot(payload);
  }
  if (stream !== "status" || typeof server_id !== "string") {
    return null;
  }
  if (typeof payload !== "object" || payload === null) {
    return null;
  }
  const state = (payload as { state?: unknown }).state;
  if (typeof state !== "string") {
    return null;
  }
  return { kind: "status", serverId: server_id, state };
}

/** Require string kind/title; detail defaults to empty and server_id may be null. */
function parseNotification(
  serverId: unknown,
  payload: unknown,
): CommunityFrame | null {
  if (serverId !== null && typeof serverId !== "string") {
    return null;
  }
  if (typeof payload !== "object" || payload === null) {
    return null;
  }
  const { kind, title, detail } = payload as {
    kind?: unknown;
    title?: unknown;
    detail?: unknown;
  };
  if (typeof kind !== "string" || typeof title !== "string") {
    return null;
  }
  return {
    kind: "notification",
    serverId,
    notificationKind: kind,
    title,
    detail: typeof detail === "string" ? detail : "",
  };
}

/** Reject a malformed snapshot entirely; a partial snapshot would misrepresent the community's server set. */
function parseSnapshot(payload: unknown): CommunityFrame | null {
  if (typeof payload !== "object" || payload === null) {
    return null;
  }
  const { servers } = payload as { servers?: unknown };
  if (!Array.isArray(servers)) {
    return null;
  }
  const parsed: StatusEvent[] = [];
  for (const entry of servers) {
    const { server_id, state } = (entry ?? {}) as {
      server_id?: unknown;
      state?: unknown;
    };
    if (typeof server_id !== "string" || typeof state !== "string") {
      return null;
    }
    parsed.push({ serverId: server_id, state });
  }
  return { kind: "snapshot", servers: parsed };
}

/** Use a new client per community and close it on community switch or sign-out. */
export class CommunityEventsClient {
  private readonly socket: EventsSocketClient;

  constructor(
    communityId: string,
    callbacks: CommunityEventsCallbacks,
    random: () => number = Math.random,
  ) {
    this.socket = new EventsSocketClient(
      () => eventsUrl(communityId),
      {
        onMessage: (raw) => {
          const frame = parseCommunityFrame(raw);
          if (frame === null) {
            return;
          }
          if (frame.kind === "gap") {
            callbacks.onGap();
            return;
          }
          if (frame.kind === "snapshot") {
            callbacks.onSnapshot(frame.servers);
            return;
          }
          if (frame.kind === "notification") {
            callbacks.onNotification({
              serverId: frame.serverId,
              notificationKind: frame.notificationKind,
              title: frame.title,
              detail: frame.detail,
            });
            return;
          }
          callbacks.onStatus({ serverId: frame.serverId, state: frame.state });
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
