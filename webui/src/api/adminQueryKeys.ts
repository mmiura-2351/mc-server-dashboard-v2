// React Query keys for the platform-admin community listing. Both the admin
// Communities page and the admin Audit community picker read GET
// /admin/communities but with different limits and purposes; keying them by the
// bare offset collided (both resolved to ["admin","communities",0] at the first
// page), so React Query deduped them and one page served the other's wrong-limit
// data. These purpose-scoped builders keep the shared ["admin","communities"]
// prefix — so a single invalidateQueries on that prefix still refreshes both —
// while embedding the full request shape so the two call sites never collide.
export const ADMIN_COMMUNITIES_KEY = ["admin", "communities"] as const;

export function adminCommunitiesListKey(limit: number, offset: number) {
  return [...ADMIN_COMMUNITIES_KEY, "list", { limit, offset }] as const;
}

export function adminCommunitiesPickerKey(limit: number) {
  return [...ADMIN_COMMUNITIES_KEY, "picker", { limit }] as const;
}
