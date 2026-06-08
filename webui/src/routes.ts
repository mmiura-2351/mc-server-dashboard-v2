// Route shapes for the authenticated shell (WEBUI_SPEC.md Section 5).
//
// The dashboard lives under a community scope (`/communities/:cid`), so the
// concrete landing depends on which community is active. Login / register / the
// route guards all bounce to LANDING_PATH, a community-agnostic route that
// resolves the active community and redirects to its dashboard (AppShell.tsx).

/** Community-agnostic post-sign-in landing; resolves to the active dashboard. */
export const LANDING_PATH = "/";

/** The dashboard route for a given community. */
export function dashboardPath(communityId: string): string {
  return `/communities/${communityId}`;
}

// The location RequireAuth stashes in router state when it bounces a signed-out
// deep link to /login (#424). Resolve it to a same-app path; ignore anything
// that isn't the router Location we set, so a stale or externally-crafted state
// can never redirect off-app — it just falls back to LANDING_PATH.
export function postLoginPath(from: unknown): string {
  if (from !== null && typeof from === "object" && "pathname" in from) {
    const { pathname, search } = from as { pathname: unknown; search: unknown };
    // Accept only a single leading "/" followed by a non-"/"/non-"\" char (or
    // the bare root). Rejecting "//" and "/\" closes a protocol-relative
    // open-redirect: a path like "//evil.com" would otherwise resolve
    // off-origin once navigate() runs (#424). The hash fragment is deliberately
    // not preserved — issue scope is path + query only.
    if (typeof pathname === "string" && /^\/(?![/\\])/.test(pathname)) {
      return `${pathname}${typeof search === "string" ? search : ""}`;
    }
  }
  return LANDING_PATH;
}

// Auth routes never make a useful return-to target: capturing /login as `next`
// would loop the user back to the login page after sign-in.
const AUTH_PATHS = ["/login", "/register"];

/**
 * Validate a `?next=…` return-to target for the session-expiry flow (#565).
 *
 * The value is the user's location at involuntary hard logout (path + query +
 * hash). It is attacker-influenceable — anyone can craft a /login?next=… link —
 * so before navigate() ever sees it we accept ONLY a same-origin in-app
 * relative path: a single leading "/" followed by a non-"/"/non-"\" char (or
 * the bare root). That rejects absolute URLs ("https://evil.com"), scheme URIs
 * ("javascript:alert(1)"), and the protocol-relative forms browsers resolve
 * off-origin ("//evil.com", "/\\evil.com"). The auth routes are rejected too so
 * a return target never loops back to /login. Returns the validated path
 * (query + hash preserved) or null when it is unusable.
 */
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

/**
 * Build the /login URL an involuntary hard logout navigates to (#565). Carries
 * `reason=expired` so the login page can explain the logout, and a validated
 * `next` (path + query + hash of where the user was) so sign-in returns them
 * there. An unusable location (e.g. an auth route) yields a bare /login?reason
 * so the user still sees the notice but lands on the default after sign-in.
 */
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
