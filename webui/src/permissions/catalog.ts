/** Community codes are role-assignable; platform codes come from the admin flag.
 * Derive the community union and editor matrix from one runtime family catalog. */

/** The role editor uses this family order; derive CommunityPermissionCode from the same catalog. */
export const COMMUNITY_PERMISSION_FAMILIES = [
  {
    family: "server",
    codes: [
      "server:create",
      "server:read",
      "server:update",
      "server:delete",
      "server:start",
      "server:stop",
      "server:restart",
      "server:command",
    ],
  },
  {
    family: "file",
    codes: ["file:read", "file:edit", "file:history", "file:rollback"],
  },
  {
    family: "backup",
    codes: [
      "backup:create",
      "backup:read",
      "backup:restore",
      "backup:delete",
      "backup:schedule",
    ],
  },
  {
    family: "schedule",
    codes: ["schedule:read", "schedule:manage"],
  },
  {
    family: "member",
    codes: ["member:read", "member:add", "member:remove"],
  },
  {
    family: "role",
    codes: ["role:read", "role:manage"],
  },
  {
    family: "grant",
    codes: ["grant:read", "grant:manage"],
  },
  {
    family: "group",
    codes: ["group:read", "group:manage"],
  },
  {
    family: "community",
    codes: ["community:read", "community:update", "community:delete"],
  },
  {
    family: "plugin",
    codes: ["plugin:read", "plugin:manage"],
  },
  {
    family: "audit",
    codes: ["audit:read"],
  },
  {
    family: "session",
    codes: ["session:read"],
  },
] as const;

export type CommunityPermissionCode =
  (typeof COMMUNITY_PERMISSION_FAMILIES)[number]["codes"][number];

/** Platform permissions come from the admin flag and cannot be assigned to community roles. */
export type PlatformPermissionCode =
  | "worker:manage"
  | "community:provision"
  | "platform:monitor";

export type PermissionCode = CommunityPermissionCode | PlatformPermissionCode;

export const COMMUNITY_PERMISSION_CODES: readonly CommunityPermissionCode[] =
  COMMUNITY_PERMISSION_FAMILIES.flatMap((f) => f.codes);
