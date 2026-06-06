/**
 * In-memory access-token store (WEBUI_SPEC.md 7.1).
 *
 * The short-lived access token lives in module state only — never
 * localStorage/sessionStorage, so it cannot be read by injected scripts and is
 * dropped on reload (the session is then re-established from the httpOnly
 * refresh cookie). The refresh token is never seen by JS; it rides the cookie.
 */

let accessToken: string | null = null;

export function getAccessToken(): string | null {
  return accessToken;
}

export function setAccessToken(token: string): void {
  accessToken = token;
}

export function clearAccessToken(): void {
  accessToken = null;
}
