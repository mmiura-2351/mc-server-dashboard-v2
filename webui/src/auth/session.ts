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
 */
type LogoutHandler = () => void;
let onHardLogout: LogoutHandler | null = null;

export function setHardLogoutHandler(fn: LogoutHandler): void {
  onHardLogout = fn;
}

/** The shared in-flight refresh, or null when none is running. */
let inFlightRefresh: Promise<boolean> | null = null;

/**
 * POST /api/auth/refresh riding the httpOnly cookie (empty JSON body). 200
 * stores the rotated access token and resolves true; 401 (signed out) resolves
 * false.
 */
async function doRefresh(): Promise<boolean> {
  let response: Response;
  try {
    response = await fetch("/api/auth/refresh", {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
  } catch {
    return false;
  }
  if (!response.ok) {
    return false;
  }
  let data: TokenResponse;
  try {
    data = (await response.json()) as TokenResponse;
  } catch {
    // A 200 with a malformed/empty body yields no usable token; treat it as a
    // failed refresh so callers hard-log-out instead of rejecting the shared
    // single-flight promise (which would strand the bootstrap).
    return false;
  }
  setAccessToken(data.access_token);
  return true;
}

/**
 * POST /api/auth/session: the non-rotating bootstrap (issue #512). Exchanges the
 * httpOnly refresh cookie for a fresh access token WITHOUT rotating the refresh
 * token, so a page load / F5 can no longer race an in-flight rotation and leave a
 * revoked predecessor cookie in the jar. Rotation stays on the periodic
 * in-session `/api/auth/refresh` path (`refreshSession`). 200 stores the access
 * token and resolves true; any failure resolves false (signed out).
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
 * 4). Resolves true when the session was re-established, false otherwise.
 */
export function refreshSession(): Promise<boolean> {
  if (inFlightRefresh === null) {
    inFlightRefresh = doRefresh().finally(() => {
      inFlightRefresh = null;
    });
  }
  return inFlightRefresh;
}

/**
 * The refresh the API client retries 401s through. On failure it drives a hard
 * logout (clear token + reset session state + navigate) and reports false so
 * the original request is not retried.
 */
export async function refreshForRetry(): Promise<boolean> {
  const ok = await refreshSession();
  if (!ok) {
    hardLogout();
  }
  return ok;
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

/** Drop local credentials and reset React state without an API round-trip. */
export function hardLogout(): void {
  clearAccessToken();
  onHardLogout?.();
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
