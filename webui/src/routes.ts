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
    if (typeof pathname === "string" && pathname.startsWith("/")) {
      return `${pathname}${typeof search === "string" ? search : ""}`;
    }
  }
  return LANDING_PATH;
}
