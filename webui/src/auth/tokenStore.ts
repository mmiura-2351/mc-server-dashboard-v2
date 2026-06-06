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

// Listeners notified whenever the token is set to a new value (login + the
// rotation on refresh, WEBUI_SPEC.md 7.1). The WS layer subscribes to reconnect
// with the fresh token on rotation (reconnect-on-rotate) without reaching into
// the session core. A plain Set keeps this dependency-free and one-directional.
const rotationListeners = new Set<() => void>();

/**
 * Subscribe to access-token rotations. The callback fires after the token is
 * replaced with a different value (not on a no-op re-set, and not on clear).
 * Returns an unsubscribe function.
 */
export function onAccessTokenRotation(listener: () => void): () => void {
  rotationListeners.add(listener);
  return () => {
    rotationListeners.delete(listener);
  };
}

export function setAccessToken(token: string): void {
  const rotated = accessToken !== null && accessToken !== token;
  accessToken = token;
  if (rotated) {
    for (const listener of rotationListeners) {
      listener();
    }
  }
}

export function clearAccessToken(): void {
  accessToken = null;
}
