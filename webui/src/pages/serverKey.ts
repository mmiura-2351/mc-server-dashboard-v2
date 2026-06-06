/** TanStack Query key for a single server's detail (cid + sid scoped). */
export function serverKey(communityId: string, serverId: string) {
  return ["server", communityId, serverId] as const;
}
