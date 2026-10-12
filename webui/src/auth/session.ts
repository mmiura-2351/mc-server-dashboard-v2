/** Call auth endpoints directly: using the API wrapper would recurse through its 401 refresh.
 * The httpOnly refresh cookie is scoped to /api/auth. */

import {
  advanceAuthEpoch,
  clearAccessToken,
  getAuthEpoch,
  setAccessToken,
} from "./tokenStore.ts";

interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

interface AccessTokenResponse {
  access_token: string;
  token_type: string;
}

/** An absent reason means deliberate logout; expiry preserves the return target in the React handler. */
export type LogoutReason = "expired";
type LogoutHandler = (reason?: LogoutReason) => void;
let onHardLogout: LogoutHandler | null = null;

export function setHardLogoutHandler(fn: LogoutHandler): void {
  onHardLogout = fn;
}

/** Inspect status explicitly: transient failures may leave a valid session, while superseded results belong to an old one. */
export type RefreshResult = {
  status: "ok" | "auth-rejected" | "transient" | "superseded";
};

let inFlightRefresh: Promise<RefreshResult> | null = null;

/** Drop the old session's shared refresh so a new session cannot join a superseded request. */
function beginEpoch(): void {
  advanceAuthEpoch();
  inFlightRefresh = null;
}

function isAuthDefinitive(status: number): boolean {
  return status === 401 || status === 403;
}

type RefreshOutcome =
  | { status: "ok"; accessToken: string }
  | { status: "auth-rejected" | "transient" };

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
    // Malformed JSON does not prove the session was rejected; allow a later refresh retry.
    return { status: "transient" };
  }
  return { status: "ok", accessToken: data.access_token };
}

async function doRefresh(): Promise<RefreshResult> {
  const epoch = getAuthEpoch();
  const outcome = await requestRefresh();
  if (epoch !== getAuthEpoch()) {
    return { status: "superseded" };
  }
  if (outcome.status === "ok") {
    setAccessToken(outcome.accessToken);
  }
  return { status: outcome.status };
}

/** Restore without rotating the cookie; discard results superseded by sign-in or logout.
 * HTTP, transport, and JSON parsing failures resolve signed-out silently. */
export async function restoreSession(): Promise<
  "signed-in" | "signed-out" | "superseded"
> {
  const epoch = getAuthEpoch();
  const accessToken = await requestSessionToken();
  if (epoch !== getAuthEpoch()) {
    return "superseded";
  }
  if (accessToken === null) {
    return "signed-out";
  }
  setAccessToken(accessToken);
  return "signed-in";
}

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
    // Malformed JSON resolves signed-out instead of rejecting bootstrap.
    return null;
  }
  return data.access_token;
}

/** Share one refresh per session so concurrent 401s cannot replay stale refresh cookies. */
export function refreshSession(): Promise<RefreshResult> {
  if (inFlightRefresh === null) {
    const flight: Promise<RefreshResult> = doRefresh().finally(() => {
      // A newer session may have started its own flight; keep it.
      if (inFlightRefresh === flight) {
        inFlightRefresh = null;
      }
    });
    inFlightRefresh = flight;
  }
  return inFlightRefresh;
}

/** Only 401/403 refresh rejection logs out; transient failures preserve the session.
 * Check the epoch again after await so an old request cannot retry or log out a newer user. */
export async function refreshForRetry(requestEpoch: number): Promise<boolean> {
  if (requestEpoch !== getAuthEpoch()) {
    return false;
  }
  const { status } = await refreshSession();
  if (requestEpoch !== getAuthEpoch()) {
    return false;
  }
  if (status === "auth-rejected") {
    hardLogout("expired");
  }
  return status === "ok";
}

/** Revoke the cookie best-effort; end the local session even if the server call fails. */
export async function logout(): Promise<void> {
  try {
    await fetch("/api/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
  } catch {
    // End the local session even if server-side logout fails.
  }
  hardLogout();
}

/** End the epoch to invalidate pending auth results; an absent reason means deliberate logout. */
export function hardLogout(reason?: LogoutReason): void {
  beginEpoch();
  clearAccessToken();
  onHardLogout?.(reason);
}

/** A fresh sign-in starts an epoch so earlier authentication results cannot replace it. */
export function signIn(accessToken: string): void {
  beginEpoch();
  setAccessToken(accessToken);
}

/** Clear session singletons between tests to avoid invoking stale router handlers. */
export function resetForTesting(): void {
  onHardLogout = null;
  inFlightRefresh = null;
}
