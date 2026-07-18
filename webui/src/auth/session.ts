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
 * Outcome of a refresh attempt. `"ok"` means the access token was
 * re-established; `"auth-rejected"` means the server authoritatively rejected
 * the session (401/403 — the refresh cookie is dead); `"transient"` means a
 * network error, proxy 5xx, or garbled body prevented the attempt but the
 * session may still be valid.
 */
export type RefreshResult = "ok" | "auth-rejected" | "transient";

/** The shared in-flight refresh, or null when none is running. */
let inFlightRefresh: Promise<RefreshResult> | null = null;

/** Auth-definitive status codes: the server says the session is dead. */
function isAuthDefinitive(status: number): boolean {
  return status === 401 || status === 403;
}

/**
 * POST /api/auth/refresh riding the httpOnly cookie (empty JSON body). 200
 * stores the rotated access token and resolves `"ok"`; 401/403 resolves
 * `"auth-rejected"` (session is genuinely dead); network errors and other
 * non-2xx responses resolve `"transient"` (session may still be valid).
 */
async function doRefresh(): Promise<RefreshResult> {
  let response: Response;
  try {
    response = await fetch("/api/auth/refresh", {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
  } catch {
    return "transient";
  }
  if (!response.ok) {
    return isAuthDefinitive(response.status) ? "auth-rejected" : "transient";
  }
  let data: TokenResponse;
  try {
    data = (await response.json()) as TokenResponse;
  } catch {
    // A 200 with a malformed/empty body yields no usable token; this is not an
    // auth rejection (the server said 200), so treat it as transient rather
    // than forcing a hard logout.
    return "transient";
  }
  setAccessToken(data.access_token);
  return "ok";
}

/**
 * POST /api/auth/session: the non-rotating bootstrap (issue #512). Exchanges the
 * httpOnly refresh cookie for a fresh access token WITHOUT rotating the refresh
 * token, so a page load / F5 can no longer race an in-flight rotation and leave a
 * revoked predecessor cookie in the jar. Rotation stays on the periodic
 * in-session `/api/auth/refresh` path (`refreshSession`). 200 stores the access
 * token and resolves true; any failure resolves false (signed out).
 *
 * A 401 here is the documented "no session" signal (the normal state on /login
 * and after logout), not an error: it must resolve false silently, never
 * `console.error`/throw (issue #641). The browser still emits its own
 * "Failed to load resource: ... 401" line for the non-2xx response — that line
 * is native and cannot be suppressed from JS; only app-level logging is in our
 * control, and there is intentionally none.
 */
export async function restoreSession(): Promise<boolean> {
  let response: Response;
  try {
    response = await fetch("/api/auth/session", {
      method: "POST",
      credentials: "same-origin",
    });
  } catch {
    return false;
  }
  if (!response.ok) {
    return false;
  }
  let data: AccessTokenResponse;
  try {
    data = (await response.json()) as AccessTokenResponse;
  } catch {
    // A 200 with a malformed/empty body yields no usable token; treat it as a
    // failed restore so the bootstrap resolves signed-out rather than rejecting.
    return false;
  }
  setAccessToken(data.access_token);
  return true;
}

/**
 * Single-flight refresh: all concurrent callers (e.g. several requests that
 * 401ed at once) share one in-flight `/api/auth/refresh`, so the client never
 * replays a stale predecessor past the API's reuse grace window (AUTH_API.md
 * 4). Resolves `"ok"` when the session was re-established, `"auth-rejected"`
 * when the server says it is dead, or `"transient"` on network/proxy errors.
 */
export function refreshSession(): Promise<RefreshResult> {
  if (inFlightRefresh === null) {
    inFlightRefresh = doRefresh().finally(() => {
      inFlightRefresh = null;
    });
  }
  return inFlightRefresh;
}

/**
 * The refresh the API client retries 401s through. On an auth-definitive
 * rejection (401/403) it drives a hard logout and reports false. On a
 * transient failure (network error, 5xx) it reports false WITHOUT logging out,
 * so the original request surfaces its own error and the session survives for
 * a later retry. On success it reports true to trigger a request retry.
 */
export async function refreshForRetry(): Promise<boolean> {
  const result = await refreshSession();
  if (result === "auth-rejected") {
    hardLogout("expired");
  }
  return result === "ok";
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
 */
export function hardLogout(reason?: LogoutReason): void {
  clearAccessToken();
  onHardLogout?.(reason);
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
