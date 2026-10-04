/**
 * Shared events-WebSocket core (WEBUI_SPEC.md 7.1, 7.2).
 *
 * The dashboard runs two event streams — the community status stream
 * (`communityEvents.ts`) and the per-server detail stream (`serverEvents.ts`) —
 * which share the exact same socket mechanics: connect with the current access
 * token in the `Sec-WebSocket-Protocol` subprotocol header, reconnect with
 * exponential backoff + jitter on loss (reset on a successful open), reconnect
 * with the fresh token after a session refresh rotates it (reconnect-on-rotate),
 * refresh the session first when the API closes the socket because its token
 * expired, and tear down cleanly. This module owns that machinery once; the two clients
 * are thin wrappers supplying a URL builder and a raw-frame handler.
 *
 * The token rides the `Sec-WebSocket-Protocol` header as
 * `["access_token", "<jwt>"]`: browsers cannot set `Authorization` on a
 * WebSocket upgrade, and putting the JWT in a query parameter leaks it into
 * access logs (#1596). The API echoes `access_token` as the accepted
 * subprotocol (RFC 6455 compliant).
 *
 * This module carries no React or TanStack Query: the wrappers supply callbacks
 * so a hook can patch the query cache without entangling this core.
 */

import { getRefresher } from "../api/client.ts";
import {
  getAccessToken,
  getAuthEpoch,
  onAccessTokenRotation,
} from "../auth/tokenStore.ts";

/**
 * The close code the API sends when the access token a socket was opened with
 * expires (WEBUI_SPEC.md 2.6, #1862). Reconnecting with that token would only
 * fail the handshake, so the session is refreshed before reconnecting.
 */
export const TOKEN_EXPIRED_CLOSE_CODE = 4419;

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

/** Build the `wss?://…` scheme+host prefix for the current page origin. */
export function wsOrigin(): string {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}`;
}

export interface EventsSocketCallbacks {
  /** A raw wire frame (the message `data` as a string). */
  onMessage: (raw: string) => void;
  /** The connection opened (resubscribe / clear degraded). */
  onOpen: () => void;
  /** The connection went down and a reconnect is pending (enter degraded). */
  onDown: () => void;
}

/**
 * Owns one events socket and its reconnect loop. Construct it with a URL builder,
 * call {@link start}, and {@link close} it on teardown. Not for reuse across
 * endpoints — make a new one per active target.
 */
export class EventsSocketClient {
  private socket: WebSocket | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  /** The token the live socket was opened with, to detect a stale connection. */
  private connectedToken: string | null = null;
  /** The token the API last closed a socket for as expired; never reused. */
  private expiredToken: string | null = null;
  private unsubscribeRotation: (() => void) | null = null;
  private stopped = false;

  constructor(
    private readonly buildUrl: () => string,
    private readonly callbacks: EventsSocketCallbacks,
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
    if (token === this.expiredToken) {
      void this.refreshThenReconnect();
      return;
    }
    this.connectedToken = token;
    const socket = new WebSocket(this.buildUrl(), ["access_token", token]);
    this.socket = socket;
    socket.onopen = () => {
      this.attempt = 0;
      this.callbacks.onOpen();
    };
    socket.onmessage = (event) => {
      this.callbacks.onMessage(String(event.data));
    };
    socket.onclose = (event) => this.onClosed(socket, event.code);
    socket.onerror = () => {
      // `error` is always followed by `close`; let `close` drive the reconnect
      // so a single failure is not counted twice.
    };
  }

  private onClosed(socket: WebSocket, code: number): void {
    if (socket !== this.socket || this.stopped) {
      return;
    }
    this.socket = null;
    this.callbacks.onDown();
    if (code === TOKEN_EXPIRED_CLOSE_CODE) {
      this.expiredToken = this.connectedToken;
      this.connect();
      return;
    }
    this.scheduleReconnect();
  }

  // The API closed a socket because its token expired: refresh through the
  // shared single-flight path (the one the API client retries 401s through)
  // before reconnecting. A refresh that rotates the token reconnects via
  // reconnect-on-rotate, usually before this resumes. One that fails retries on
  // the backoff (each attempt refreshes again, never offering the expired
  // token); an auth-definitive failure hard-logs-out, which closes this client.
  // The refresh is scoped to the session the expired token belonged to, so one
  // that session outlives never logs a newer session out (#3224). No epoch
  // re-check is needed before reconnecting: `connect` reads the current token,
  // which only the current session can have set.
  private async refreshThenReconnect(): Promise<void> {
    const refresh = getRefresher();
    const refreshed = refresh !== null && (await refresh(getAuthEpoch()));
    if (this.stopped || this.socket !== null || this.timer !== null) {
      return;
    }
    if (refreshed && getAccessToken() !== this.expiredToken) {
      this.connect();
    } else {
      this.scheduleReconnect();
    }
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
  // one and the API will close it when that token expires, so reconnect now
  // with the fresh token (reconnect-on-rotate, WEBUI_SPEC.md 7.1). A pending backoff is
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
