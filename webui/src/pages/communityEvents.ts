/**
 * Community events WebSocket client (WEBUI_SPEC.md 7.2, 7.6).
 *
 * A framework-free wrapper over the plain `WebSocket` (no library) for the
 * dashboard's one community-scoped events stream
 * (`WS /communities/{cid}/events`, STATUS only). It owns the socket lifecycle:
 * connect with the current access token, parse the API's status frames, and
 * reconnect with exponential backoff + jitter on loss — resetting the backoff
 * on a successful open and reconnecting with the fresh token after a session
 * refresh rotates it (reconnect-on-rotate, WEBUI_SPEC.md 7.1).
 *
 * The token rides the `?token=` query parameter: browsers cannot set request
 * headers on a WebSocket upgrade, so the API accepts the access token there
 * (`_ws_access_token` in api/.../dependencies.py).
 *
 * This module carries no React or TanStack Query: the caller supplies callbacks
 * for status frames, the unknown-server hint, and the degraded transitions, so
 * the dashboard hook can patch the query cache without entangling this client.
 */

import { getAccessToken, onAccessTokenRotation } from "../auth/tokenStore.ts";

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

/**
 * Backoff bounds (WEBUI_SPEC.md 7.2). Exponential from a small base, capped, so
 * a flapping or down API is retried promptly at first and then backs off to a
 * steady ~30s probe instead of hammering.
 */
const BASE_DELAY_MS = 1000;
const MAX_DELAY_MS = 30000;

/**
 * The reconnect delay for a given consecutive-failure count: an exponential
 * step capped at {@link MAX_DELAY_MS}, then full jitter in `[0, step]` so a
 * fleet of clients does not reconnect in lockstep after a shared outage.
 *
 * `attempt` is 1-based (the first reconnect is attempt 1). `random` is injected
 * for deterministic tests; it defaults to `Math.random` and must return a value
 * in `[0, 1)`.
 */
export function backoffDelayMs(attempt: number, random: () => number): number {
  const step = Math.min(BASE_DELAY_MS * 2 ** (attempt - 1), MAX_DELAY_MS);
  return Math.floor(random() * step);
}

/** Build the `wss?://…/communities/{cid}/events?token=…` URL for `cid`. */
function eventsUrl(communityId: string, token: string): string {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  const path = `/communities/${encodeURIComponent(communityId)}/events`;
  const query = `?token=${encodeURIComponent(token)}`;
  return `${scheme}//${window.location.host}${path}${query}`;
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
  private socket: WebSocket | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  /** The token the live socket was opened with, to detect a stale connection. */
  private connectedToken: string | null = null;
  private unsubscribeRotation: (() => void) | null = null;
  private stopped = false;

  constructor(
    private readonly communityId: string,
    private readonly callbacks: CommunityEventsCallbacks,
    private readonly random: () => number = Math.random,
  ) {}

  /** Open the socket and arm the reconnect loop. Idempotent per instance. */
  start(): void {
    this.unsubscribeRotation = onAccessTokenRotation(() => this.onRotation());
    this.connect();
  }

  /** Tear down: stop reconnecting, drop rotation hook, close the socket. */
  close(): void {
    this.stopped = true;
    this.unsubscribeRotation?.();
    this.unsubscribeRotation = null;
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.teardownSocket();
  }

  private connect(): void {
    if (this.stopped) {
      return;
    }
    const token = getAccessToken();
    if (token === null) {
      // No credential to connect with (e.g. mid sign-out). Stay degraded and
      // retry on the backoff; a rotation will also re-arm us promptly.
      this.callbacks.onDown();
      this.scheduleReconnect();
      return;
    }
    this.connectedToken = token;
    const socket = new WebSocket(eventsUrl(this.communityId, token));
    this.socket = socket;
    socket.onopen = () => {
      this.attempt = 0;
      this.callbacks.onOpen();
    };
    socket.onmessage = (event) => {
      const status = parseStatusFrame(String(event.data));
      if (status !== null) {
        this.callbacks.onStatus(status);
      }
    };
    socket.onclose = () => this.onClosed(socket);
    socket.onerror = () => {
      // `error` is always followed by `close`; let `close` drive the reconnect
      // so a single failure is not counted twice.
    };
  }

  private onClosed(socket: WebSocket): void {
    if (socket !== this.socket || this.stopped) {
      return;
    }
    this.socket = null;
    this.callbacks.onDown();
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.timer !== null) {
      return;
    }
    this.attempt += 1;
    const delay = backoffDelayMs(this.attempt, this.random);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.connect();
    }, delay);
  }

  // A refresh rotated the access token: the live socket still carries the old
  // one and the API will close it at the 60s re-auth, so reconnect now with the
  // fresh token (reconnect-on-rotate, WEBUI_SPEC.md 7.1). A pending backoff is
  // collapsed so the new token is used immediately rather than after the wait.
  private onRotation(): void {
    if (this.stopped || getAccessToken() === this.connectedToken) {
      return;
    }
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.teardownSocket();
    this.attempt = 0;
    this.connect();
  }

  private teardownSocket(): void {
    if (this.socket !== null) {
      // Drop our handlers first so the deliberate close does not re-enter the
      // reconnect loop, then close.
      this.socket.onopen = null;
      this.socket.onmessage = null;
      this.socket.onclose = null;
      this.socket.onerror = null;
      this.socket.close();
      this.socket = null;
    }
  }
}
