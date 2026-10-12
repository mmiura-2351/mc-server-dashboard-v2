/** Authenticate with ["access_token", jwt] subprotocols because browsers cannot set WebSocket Authorization headers.
 * Keep tokens out of query strings, which can leak into access logs. */

import { getRefresher } from "../api/client.ts";
import {
  getAccessToken,
  getAuthEpoch,
  onAccessTokenRotation,
} from "../auth/tokenStore.ts";

/** Refresh before reconnecting after token expiry; reusing the expired token fails the handshake. */
export const TOKEN_EXPIRED_CLOSE_CODE = 4419;

/** Refresh on mid-stream account rejection so a revoked session can log out instead of reconnecting forever. */
export const UNAUTHENTICATED_CLOSE_CODE = 4401;

const BASE_DELAY_MS = 1000;
const MAX_DELAY_MS = 30000;

/** Use capped exponential backoff with full jitter to spread reconnects after shared outages.
 * Attempts are 1-based; the injected random function must return a value in [0, 1). */
export function backoffDelayMs(attempt: number, random: () => number): number {
  const step = Math.min(BASE_DELAY_MS * 2 ** (attempt - 1), MAX_DELAY_MS);
  return Math.floor(random() * step);
}

export function wsOrigin(): string {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}`;
}

export interface EventsSocketCallbacks {
  onMessage: (raw: string) => void;
  onOpen: () => void;
  onDown: () => void;
}

/** Use a new client per target; start once and close on teardown. */
export class EventsSocketClient {
  private socket: WebSocket | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private connectedToken: string | null = null;
  /** Never reconnect with the token the API last rejected or declared expired. */
  private expiredToken: string | null = null;
  private unsubscribeRotation: (() => void) | null = null;
  private stopped = false;

  constructor(
    private readonly buildUrl: () => string,
    private readonly callbacks: EventsSocketCallbacks,
    private readonly random: () => number = Math.random,
  ) {}

  start(): void {
    this.unsubscribeRotation = onAccessTokenRotation(() => this.onRotation());
    this.connect();
  }

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
      // Without a token, stay degraded and retry with backoff until credentials become available.
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
      // Let close drive reconnection after error so one failure is counted only once.
    };
  }

  private onClosed(socket: WebSocket, code: number): void {
    if (socket !== this.socket || this.stopped) {
      return;
    }
    this.socket = null;
    this.callbacks.onDown();
    if (
      code === TOKEN_EXPIRED_CLOSE_CODE ||
      code === UNAUTHENTICATED_CLOSE_CODE
    ) {
      this.expiredToken = this.connectedToken;
      this.connect();
      return;
    }
    this.scheduleReconnect();
  }

  // Refresh rejected tokens through the session-scoped single-flight path before reconnecting.
  // Rotation may reconnect before await resumes; otherwise retry with backoff without reusing the rejected token.
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

  // Reconnect immediately with the rotated token rather than waiting for expiry or pending backoff.
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
      // Detach handlers before deliberate close so teardown cannot schedule a reconnect.
      this.socket.onopen = null;
      this.socket.onmessage = null;
      this.socket.onclose = null;
      this.socket.onerror = null;
      this.socket.close();
      this.socket = null;
    }
  }
}
