/**
 * Community events WebSocket client (WEBUI_SPEC.md 7.2, 7.6).
 *
 * A thin wrapper over the shared {@link EventsSocketClient} core
 * (`eventsSocket.ts`) for the dashboard's one community-scoped events stream
 * (`WS /communities/{cid}/events`, STATUS only). The core owns the socket
 * lifecycle (connect, backoff reconnect, reconnect-on-rotate, teardown); this
 * module supplies the community URL and parses the API's status frames and the
 * GAP marker (the client fell behind and frames were dropped).
 *
 * This module carries no React or TanStack Query: the caller supplies callbacks
 * for status frames and the degraded transitions, so the dashboard hook can
 * patch the query cache without entangling this client.
 */

import {
  backoffDelayMs,
  EventsSocketClient,
  wsOrigin,
} from "./eventsSocket.ts";

// Re-exported for the existing tests / callers that import it from here.
export { backoffDelayMs };

/** A STATUS frame from the community stream (`_community_frame` in events.py). */
export interface StatusEvent {
  serverId: string;
  state: string;
}

/**
 * A NOTIFICATION frame from the community stream (`EventStream.NOTIFICATION`,
 * issue #1836). Its payload is the canonical `{kind, title, detail}` operator
 * notice; `server_id` rides the community frame (null for a server-agnostic
 * notice). Today the only kind is `schedule_failed` (#1838).
 */
export interface NotificationEvent {
  serverId: string | null;
  /** The `payload.kind` discriminator, e.g. `schedule_failed`. */
  notificationKind: string;
  title: string;
  detail: string;
}

/**
 * A parsed community-stream frame: a routable STATUS, a NOTIFICATION, or the
 * GAP marker.
 */
export type CommunityFrame =
  | ({ kind: "status" } & StatusEvent)
  | ({ kind: "notification" } & NotificationEvent)
  | { kind: "gap" };

export interface CommunityEventsCallbacks {
  /** A parsed STATUS frame for a known server in this community. */
  onStatus: (event: StatusEvent) => void;
  /** A parsed NOTIFICATION frame (operator notice, e.g. a schedule failure). */
  onNotification: (event: NotificationEvent) => void;
  /** The stream fell behind and dropped frames (slow-client overflow). */
  onGap: () => void;
  /** The connection opened (resubscribe / clear degraded). */
  onOpen: () => void;
  /** The connection went down and a reconnect is pending (enter degraded). */
  onDown: () => void;
}

/** Build the `wss?://…/api/communities/{cid}/events` URL for `cid`. */
function eventsUrl(communityId: string): string {
  return `${wsOrigin()}/api/communities/${encodeURIComponent(communityId)}/events`;
}

/**
 * Parse a wire frame into a {@link CommunityFrame}, or null when it is neither
 * a routable STATUS/NOTIFICATION frame nor the GAP marker (an unknown stream or
 * a malformed body). The GAP marker is server-agnostic (`server_id` is null): it
 * means frames were dropped for an unknown set of servers, so the caller must
 * reconcile the whole list.
 */
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

/**
 * Parse a NOTIFICATION frame. `server_id` may be null (a server-agnostic
 * notice); the payload must carry a string `kind` and `title`. `detail` is
 * optional and defaults to the empty string.
 */
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

/**
 * Owns one community events socket and its reconnect loop. Construct it, call
 * {@link start}, and {@link close} it on community switch / sign-out. Not for
 * reuse across communities — make a new one per active community id.
 */
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

  /** Open the socket and arm the reconnect loop. Idempotent per instance. */
  start(): void {
    this.socket.start();
  }

  /** Tear down: stop reconnecting, drop rotation hook, close the socket. */
  close(): void {
    this.socket.close();
  }
}
