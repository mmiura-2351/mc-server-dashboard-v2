/**
 * Capability fetching (WEBUI_SPEC.md 7.3).
 *
 * `GET /communities/{cid}/me/permissions` is fetched when the active community
 * changes and cached for the session via the TanStack Query cache, keyed by
 * community id so each community keeps its own set. A 403 re-fetch is driven by
 * invalidating this key (see {@link refetchCapabilities}).
 */

import type { QueryClient } from "@tanstack/react-query";
import { api } from "../api/client.ts";
import type { EffectivePermissions } from "./resolve.ts";

const PERMISSIONS_PATH = "/communities/{community_id}/me/permissions" as const;

/** Query key for one community's effective permission set. */
export function capabilitiesKey(communityId: string) {
  return ["capabilities", communityId] as const;
}

export function fetchCapabilities(
  communityId: string,
): Promise<EffectivePermissions> {
  // The minimal client (api/client.ts) has no path-param interpolation, so the
  // concrete URL is built here while the literal path keeps the response typed.
  const url = `/communities/${encodeURIComponent(communityId)}/me/permissions`;
  return api.get(url as typeof PERMISSIONS_PATH);
}

/**
 * Re-fetch the active community's capabilities. Called from the 403 glue: the
 * cached set may be stale (a role/grant changed since it was loaded), so the
 * next `can()` reflects the server's current answer.
 */
export function refetchCapabilities(
  queryClient: QueryClient,
  communityId: string,
): Promise<void> {
  return queryClient.invalidateQueries({
    queryKey: capabilitiesKey(communityId),
  });
}
