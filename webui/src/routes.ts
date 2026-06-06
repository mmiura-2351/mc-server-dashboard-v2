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
