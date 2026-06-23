/** React-query cache keys for the plugin management endpoints. */

export function pluginsKey(communityId: string, serverId: string) {
  return ["plugins", communityId, serverId] as const;
}

export function pluginUpdatesKey(communityId: string, serverId: string) {
  return ["plugins", communityId, serverId, "updates"] as const;
}

export function pluginValidationKey(communityId: string, serverId: string) {
  return ["plugins", communityId, serverId, "validation"] as const;
}

export function catalogSearchKey(
  communityId: string,
  serverId: string,
  query: string,
) {
  return ["catalog", communityId, serverId, "search", query] as const;
}

export function catalogProjectKey(
  communityId: string,
  serverId: string,
  projectIdOrSlug: string,
) {
  return [
    "catalog",
    communityId,
    serverId,
    "project",
    projectIdOrSlug,
  ] as const;
}
