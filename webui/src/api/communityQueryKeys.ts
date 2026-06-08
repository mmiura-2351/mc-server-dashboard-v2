// Shared React Query key factories for community entities that several tabs
// read at once. Routing every query and invalidation through these makes cache
// invalidation compose across tabs: a mutation in one tab marks the matching
// queries stale in every sibling tab, because partial (prefix) invalidation in
// React Query matches by the leading key segments and all builders for an
// entity keep a common prefix.
//
// Why this exists (issue #473):
//   - Roles: CommunityRolesTab and CommunityMembersTab both read the role list,
//     and deleting a role must also refresh the Members tab's role chips, which
//     are rendered from each member's roles in the *members* list. So a role
//     mutation invalidates both rolesKeys and membersKeys.
//   - Group/attachment data: CommunityGroupsTab and ServerPlayersTab used to key
//     the same group list and the same group<->server attachment relation under
//     disjoint namespaces, so a mutation in one tab never invalidated the other.
//     They now share these factories.

const COMMUNITIES = "communities" as const;

/** Members of a community (GET /communities/{id}/members). */
export const membersKeys = {
  list(communityId: string) {
    return [COMMUNITIES, communityId, "members"] as const;
  },
};

/** Roles of a community (GET /communities/{id}/roles). */
export const rolesKeys = {
  list(communityId: string) {
    return [COMMUNITIES, communityId, "roles"] as const;
  },
};

/** Groups of a community (GET /communities/{id}/groups). */
export const groupsKeys = {
  list(communityId: string) {
    return [COMMUNITIES, communityId, "groups"] as const;
  },
};

// The group<->server attachment relation. Two endpoints project the same
// relation from opposite ends: a group's servers (Groups tab) and a server's
// groups (Players tab). An attach/detach changes both, so both builders share
// the all(communityId) prefix and a single invalidate on that prefix refreshes
// either projection wherever it is mounted.
export const attachmentsKeys = {
  all(communityId: string) {
    return [COMMUNITIES, communityId, "attachments"] as const;
  },
  /** Servers attached to one group. */
  forGroup(communityId: string, groupId: string) {
    return [...attachmentsKeys.all(communityId), "group", groupId] as const;
  },
  /** Groups attached to one server. */
  forServer(communityId: string, serverId: string) {
    return [...attachmentsKeys.all(communityId), "server", serverId] as const;
  },
};
