/** React-query cache keys for the backup endpoints. */

/** Backups list query key — scoped to the server, invalidated on every change. */
export function backupsKey(communityId: string, serverId: string) {
  return ["backups", communityId, serverId] as const;
}

/** Backups statistics query key. */
export function backupStatsKey(communityId: string, serverId: string) {
  return ["backups", communityId, serverId, "statistics"] as const;
}
