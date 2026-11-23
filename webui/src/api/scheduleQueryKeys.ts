/** React-query cache keys for the schedule endpoints. */

/** Schedules list query key — scoped to the server, invalidated on change. */
export function schedulesKey(communityId: string, serverId: string) {
  return ["schedules", communityId, serverId] as const;
}
