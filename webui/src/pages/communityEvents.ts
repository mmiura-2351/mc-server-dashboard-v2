/**
 * Community events WebSocket client (WEBUI_SPEC.md 7.2, 7.6).
 *
 * A thin wrapper over the shared {@link EventsSocketClient} core
 * (`eventsSocket.ts`) for the dashboard's one community-scoped events stream
 * (`WS /communities/{cid}/events`, STATUS only). The core owns the socket
 * lifecycle (connect, backoff reconnect, reconnect-on-rotate, teardown); this
 * module supplies the community URL and parses the API's status frames.
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

export interface CommunityEventsCallbacks {
  /** A parsed STATUS frame for a known server in this community. */
  onStatus: (event: StatusEvent) => void;
  /** The connection opened (resubscribe / clear degraded). */
  onOpen: () => void;
  /** The connection went down and a reconnect is pending (enter degraded). */
  onDown: () => void;
}

/** Build the `wss?://…/communities/{cid}/events?token=…` URL for `cid`. */
function eventsUrl(communityId: string, token: string): string {
  const path = `/communities/${encodeURIComponent(communityId)}/events`;
  const query = `?token=${encodeURIComponent(token)}`;
  return `${wsOrigin()}${path}${query}`;
}

/**
 * Parse a wire frame into a {@link StatusEvent}, or null when it is not a
 * routable STATUS frame (the server-agnostic GAP marker, a non-status stream,
 * or a malformed body). A GAP frame carries no `server_id`, so it is dropped
 * here — the dashboard already reconciles via the list refetch path.
 */
export function parseStatusFrame(raw: string): StatusEvent | null {
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
  return { serverId: server_id, state };
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
      (token) => eventsUrl(communityId, token),
      {
        onMessage: (raw) => {
          const status = parseStatusFrame(raw);
          if (status !== null) {
            callbacks.onStatus(status);
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
