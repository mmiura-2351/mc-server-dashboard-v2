const COMMUNITIES = "communities" as const;

export const membersKeys = {
  list(communityId: string) {
    return [COMMUNITIES, communityId, "members"] as const;
  },
};

export const rolesKeys = {
  list(communityId: string) {
    return [COMMUNITIES, communityId, "roles"] as const;
  },
};

export const groupsKeys = {
  list(communityId: string) {
    return [COMMUNITIES, communityId, "groups"] as const;
  },
};

// Group and server attachment queries share an invalidation prefix because either mutation changes both views.
export const attachmentsKeys = {
  all(communityId: string) {
    return [COMMUNITIES, communityId, "attachments"] as const;
  },
  forGroup(communityId: string, groupId: string) {
    return [...attachmentsKeys.all(communityId), "group", groupId] as const;
  },
  forServer(communityId: string, serverId: string) {
    return [...attachmentsKeys.all(communityId), "server", serverId] as const;
  },
};
