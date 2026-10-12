// Include purpose and page size in list keys to prevent cross-page cache collisions.
// Keep a shared prefix so mutations can invalidate both listings.
export const ADMIN_COMMUNITIES_KEY = ["admin", "communities"] as const;

export function adminCommunitiesListKey(limit: number, offset: number) {
  return [...ADMIN_COMMUNITIES_KEY, "list", { limit, offset }] as const;
}

export function adminCommunitiesPickerKey(limit: number) {
  return [...ADMIN_COMMUNITIES_KEY, "picker", { limit }] as const;
}
