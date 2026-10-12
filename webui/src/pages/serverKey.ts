export function serverKey(communityId: string, serverId: string) {
  return ["server", communityId, serverId] as const;
}
