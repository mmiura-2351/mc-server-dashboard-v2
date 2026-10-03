/**
 * Session core (WEBUI_SPEC.md 7.1, AUTH_API.md).
 *
 * Owns the refresh/logout HTTP calls and the single-flight refresh mutex that
 * the API client retries 401s through. These talk to `/api/auth/*` directly
 * rather than through the typed `api` wrapper: the wrapper retries 401s via
 * refresh, which would recurse, and the cookie-based refresh has no useful typed
 * body for cookie clients (it ignores the body's refresh_token, AUTH_API.md 3).
 *
 * The refresh cookie is httpOnly with `Path=/api/auth`, so the browser only
 * sends it to these endpoints, and only when `credentials` are included.
 */

import { clearAccessToken, setAccessToken } from "./tokenStore.ts";

interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

interface AccessTokenResponse {
  access_token: string;
  token_type: string;
}

/**
 * The React layer registers what a hard logout does to its state (reset to
 * signed-out, navigate to /login). Kept as a hook so this module stays free of
 * React and routing.
 *
 * `reason` distinguishes an involuntary expiry (a transparent refresh that
 * 401ed) from a deliberate user logout, so only the involuntary path captures a
 * return-to location and shows the "session expired" notice (#565).
 */
export type LogoutReason = "expired";
type LogoutHandler = (reason?: LogoutReason) => void;
let onHardLogout: LogoutHandler | null = null;

export function setHardLogoutHandler(fn: LogoutHandler): void {
  onHardLogout = fn;
}

/**
 * Outcome of a refresh attempt. Wrapped in an object so that callers cannot
 * accidentally rely on truthiness — all string values would be truthy, making
 * `if (await refreshSession())` silently wrong (issue #2160). Callers must
 * inspect `.status` explicitly.
 *
 * - `"ok"` — access token re-established.
 * - `"auth-rejected"` — server authoritatively rejected the session (401/403).
 * - `"transient"` — network error, proxy 5xx, or garbled body; session may
 *   still be valid.
 * - `"superseded"` — the session ended (logout) or was replaced (sign-in)
 *   while the refresh was in flight; its outcome belonged to the old session
 *   and was discarded (#3224).
 */
export type RefreshResult = {
  status: "ok" | "auth-rejected" | "transient" | "superseded";
};

/**
 * The authentication epoch: bumped whenever the local session ends (logout) or
 * a new one begins (sign-in). Every token-adopting request records the epoch it
 * started in and discards its result if the epoch has moved on by the time it
 * lands, so a late response for user A can neither revive A after logout nor
 * overwrite user B's session, and a late rejection cannot log B out (#3224).
 */
let authEpoch = 0;

/** The shared in-flight refresh, or null when none is running. */
let inFlightRefresh: Promise<RefreshResult> | null = null;

/**
 * Start a new authentication epoch. The in-flight refresh belongs to the old
 * one, so it is dropped: a caller in the new session starts its own refresh
 * instead of joining one whose outcome will be discarded.
 */
function beginEpoch(): void {
  authEpoch += 1;
  inFlightRefresh = null;
}

/** Auth-definitive status codes: the server says the session is dead. */
function isAuthDefinitive(status: number): boolean {
  return status === 401 || status === 403;
}

/** A refresh exchange's outcome, before any token is adopted. */
type RefreshOutcome =
  | { status: "ok"; accessToken: string }
  | { status: "auth-rejected" | "transient" };

/**
 * POST /api/auth/refresh riding the httpOnly cookie (empty JSON body). 200
 * yields the rotated access token (`"ok"`); 401/403 yield `"auth-rejected"`
 * (session is genuinely dead); network errors and other non-2xx responses
 * yield `"transient"` (session may still be valid). Stores nothing.
 */
async function requestRefresh(): Promise<RefreshOutcome> {
  let response: Response;
  try {
    response = await fetch("/api/auth/refresh", {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
  } catch {
    return { status: "transient" };
  }
  if (!response.ok) {
    return {
      status: isAuthDefinitive(response.status) ? "auth-rejected" : "transient",
    };
  }
  let data: TokenResponse;
  try {
    data = (await response.json()) as TokenResponse;
  } catch {
    // A 200 with a malformed/empty body yields no usable token; this is not an
    // auth rejection (the server said 200), so treat it as transient rather
    // than forcing a hard logout.
    return { status: "transient" };
  }
  return { status: "ok", accessToken: data.access_token };
}

/**
 * Run one refresh exchange and store the rotated access token on success,
 * unless the session changed while it was in flight.
 */
async function doRefresh(): Promise<RefreshResult> {
  const epoch = authEpoch;
  const outcome = await requestRefresh();
  if (epoch !== authEpoch) {
    return { status: "superseded" };
  }
  if (outcome.status === "ok") {
    setAccessToken(outcome.accessToken);
  }
  return { status: outcome.status };
}

/**
 * POST /api/auth/session: the non-rotating bootstrap (issue #512). Exchanges the
 * httpOnly refresh cookie for a fresh access token WITHOUT rotating the refresh
 * token, so a page load / F5 can no longer race an in-flight rotation and leave a
 * revoked predecessor cookie in the jar. Rotation stays on the periodic
 * in-session `/api/auth/refresh` path (`refreshSession`). 200 stores the access
 * token and resolves `"signed-in"`; any failure resolves `"signed-out"`. If the
 * session changed (logout or sign-in) while the probe was in flight, its result
 * belongs to the old session: nothing is stored and it resolves `"superseded"`,
 * which the caller must not apply (#3224).
 *
 * A 401 here is the documented "no session" signal (the normal state on /login
 * and after logout), not an error: it must resolve signed-out silently, never
 * `console.error`/throw (issue #641). The browser still emits its own
 * "Failed to load resource: ... 401" line for the non-2xx response — that line
 * is native and cannot be suppressed from JS; only app-level logging is in our
 * control, and there is intentionally none.
 */
export async function restoreSession(): Promise<
  "signed-in" | "signed-out" | "superseded"
> {
  const epoch = authEpoch;
  const accessToken = await requestSessionToken();
  if (epoch !== authEpoch) {
    return "superseded";
  }
  if (accessToken === null) {
    return "signed-out";
  }
  setAccessToken(accessToken);
  return "signed-in";
}

/** POST /api/auth/session: the access token on a 200, else null. */
async function requestSessionToken(): Promise<string | null> {
  let response: Response;
  try {
    response = await fetch("/api/auth/session", {
      method: "POST",
      credentials: "same-origin",
    });
  } catch {
    return null;
  }
  if (!response.ok) {
    return null;
  }
  let data: AccessTokenResponse;
  try {
    data = (await response.json()) as AccessTokenResponse;
  } catch {
    // A 200 with a malformed/empty body yields no usable token; treat it as a
    // failed restore so the bootstrap resolves signed-out rather than rejecting.
    return null;
  }
  return data.access_token;
}

/**
 * Single-flight refresh: all concurrent callers (e.g. several requests that
 * 401ed at once) share one in-flight `/api/auth/refresh`, so the client never
 * replays a stale predecessor past the API's reuse grace window (AUTH_API.md
 * 4). Resolves `{ status: "ok" }` when the session was re-established,
 * `{ status: "auth-rejected" }` when the server says it is dead,
 * `{ status: "transient" }` on network/proxy errors, or
 * `{ status: "superseded" }` when the session changed while it was in flight.
 * The flight is scoped to one session: a new epoch drops it (`beginEpoch`), so
 * only same-session callers ever share it.
 */
export function refreshSession(): Promise<RefreshResult> {
  if (inFlightRefresh === null) {
    const flight: Promise<RefreshResult> = doRefresh().finally(() => {
      // A newer session may have started its own flight meanwhile; keep it.
      if (inFlightRefresh === flight) {
        inFlightRefresh = null;
      }
    });
    inFlightRefresh = flight;
  }
  return inFlightRefresh;
}

/**
 * The refresh the API client retries 401s through. On an auth-definitive
 * rejection (401/403) it drives a hard logout and reports false. On a
 * transient failure (network error, 5xx) it reports false WITHOUT logging out,
 * so the original request surfaces its own error and the session survives for
 * a later retry. On success it reports true to trigger a request retry. A
 * superseded refresh reports false and does nothing else: the request that
 * triggered it belonged to a session that no longer exists, so it must neither
 * be retried as the new user nor log the new user out.
 */
export async function refreshForRetry(): Promise<boolean> {
  const { status } = await refreshSession();
  if (status === "auth-rejected") {
    hardLogout("expired");
  }
  return status === "ok";
}

/**
 * Hard logout (WEBUI_SPEC.md 7.1): tell the API to revoke + clear the cookie
 * (idempotent 204), drop the in-memory token, and reset the React session
 * state. The server call is best-effort — local state is reset regardless.
 */
export async function logout(): Promise<void> {
  try {
    await fetch("/api/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
  } catch {
    // Best-effort: a failed/blocked logout call still ends the local session.
  }
  hardLogout();
}

/**
 * Drop local credentials and reset React state without an API round-trip. A
 * `reason` is forwarded to the React handler so an involuntary expiry can be
 * told apart from a deliberate logout; an absent reason is a deliberate logout.
 * Ends the authentication epoch, so nothing still in flight can revive the
 * session.
 */
export function hardLogout(reason?: LogoutReason): void {
  beginEpoch();
  clearAccessToken();
  onHardLogout?.(reason);
}

/**
 * Adopt the access token issued by a fresh /auth/login (or registration). Starts
 * a new authentication epoch, so a response still in flight from an earlier
 * session can never replace this one or log it out.
 */
export function signIn(accessToken: string): void {
  beginEpoch();
  setAccessToken(accessToken);
}

/**
 * Reset this module's state for tests. The injected hard-logout handler and the
 * in-flight refresh are module-level singletons that otherwise survive across
 * test cases/files; a leftover handler bound to an unmounted render makes a
 * later logout navigate a stale router. Tests call this per case to isolate.
 */
export function resetForTesting(): void {
  onHardLogout = null;
  inFlightRefresh = null;
}
