/** Keep access tokens out of persistent browser storage; reload restores from the httpOnly cookie.
 * Module state does not protect the token from scripts executing in the page. */

let accessToken: string | null = null;

export function getAccessToken(): string | null {
  return accessToken;
}

// WebSocket clients subscribe here to reconnect after an existing token rotates.
const rotationListeners = new Set<() => void>();

/** Notify only when an existing token changes; initial assignment, clearing, and no-op sets do not notify. */
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

/** Sign-in and logout advance the epoch; asynchronous auth results apply only to their original epoch. */
let authEpoch = 0;

export function getAuthEpoch(): number {
  return authEpoch;
}

export function advanceAuthEpoch(): void {
  authEpoch += 1;
}
