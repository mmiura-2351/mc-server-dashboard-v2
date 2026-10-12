/** The landing route resolves the active community before redirecting to its dashboard. */
export const LANDING_PATH = "/";

export function dashboardPath(communityId: string): string {
  return `/communities/${communityId}`;
}

// Resolve the signed-out guard's saved location to an in-app path, with landing as fallback.
export function postLoginPath(from: unknown): string {
  if (from !== null && typeof from === "object" && "pathname" in from) {
    const { pathname, search } = from as { pathname: unknown; search: unknown };
    // Reject protocol-relative paths beginning // or /\\ to prevent off-origin redirects.
    // Preserve the query only; this guard path omits the hash.
    if (typeof pathname === "string" && /^\/(?![/\\])/.test(pathname)) {
      return `${pathname}${typeof search === "string" ? search : ""}`;
    }
  }
  return LANDING_PATH;
}

const AUTH_PATHS = ["/login", "/register"];

/** Accept only in-app paths with one leading slash; reject auth routes to avoid return loops.
 * Preserve query and hash for valid expiry targets. */
export function safeNextPath(raw: unknown): string | null {
  if (typeof raw !== "string" || !/^\/(?![/\\])/.test(raw)) {
    return null;
  }
  const path = raw.split(/[?#]/, 1)[0];
  if (AUTH_PATHS.includes(path)) {
    return null;
  }
  return raw;
}

/** Preserve the expiry notice and validated return location; unusable targets fall back to landing after sign-in. */
export function expiredLoginPath(location: {
  pathname: string;
  search: string;
  hash: string;
}): string {
  const params = new URLSearchParams({ reason: "expired" });
  const next = safeNextPath(
    `${location.pathname}${location.search}${location.hash}`,
  );
  if (next !== null) {
    params.set("next", next);
  }
  return `/login?${params.toString()}`;
}
