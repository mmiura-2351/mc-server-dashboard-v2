import type { QueryClient } from "@tanstack/react-query";
import { api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { EffectivePermissions } from "./resolve.ts";

export function capabilitiesKey(communityId: string) {
  return ["capabilities", communityId] as const;
}

export function fetchCapabilities(
  communityId: string,
  signal?: AbortSignal,
): Promise<EffectivePermissions> {
  return api.get(
    apiPath("/api/communities/{community_id}/me/permissions", {
      community_id: communityId,
    }),
    { signal },
  );
}

/** A 403 may mean roles or grants changed; invalidate cached capabilities before the next permission check. */
export function refetchCapabilities(
  queryClient: QueryClient,
  communityId: string,
): Promise<void> {
  return queryClient.invalidateQueries({
    queryKey: capabilitiesKey(communityId),
  });
}
