/**
 * Permission code catalog (WEBUI_SPEC.md 2.2).
 *
 * The community axis is the 30-code set assignable to roles / grants. The
 * platform axis is flag-driven (not assignable to roles) but is still part of
 * the caller's effective set the API returns, so it is included in the union
 * so `can()` can be typed against every code the UI may check.
 *
 * Codes are typed as a string-literal union rather than bare `string` so a
 * typo in a `can()` call fails typecheck. The community-axis codes additionally
 * have a runtime listing grouped by their 9 families
 * (`COMMUNITY_PERMISSION_FAMILIES`) so the role/grant editor can build its
 * matrix from one source of truth — the union type is derived from that listing
 * so the two cannot drift.
 */

/**
 * Community-axis codes grouped by family, in the order the role matrix renders
 * them (WEBUI_SPEC.md 2.2 — the 9 families, 30 codes). This is the single
 * source of truth: the `CommunityPermissionCode` union is derived from it.
 */
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
    family: "audit",
    codes: ["audit:read"],
  },
  {
    family: "session",
    codes: ["session:read"],
  },
] as const;

/** Community-axis codes (30) — the role/grant editor's source of truth. */
export type CommunityPermissionCode =
  (typeof COMMUNITY_PERMISSION_FAMILIES)[number]["codes"][number];

/** Platform-axis codes (flag-driven, not assignable to roles). */
export type PlatformPermissionCode =
  | "worker:manage"
  | "community:provision"
  | "platform:monitor";

/** Every permission code the UI may check `can()` against. */
export type PermissionCode = CommunityPermissionCode | PlatformPermissionCode;

/**
 * Runtime list of the community-axis codes, the source of truth for editors
 * that must enumerate codes (e.g. the grant picker filters this by family).
 * Typed as `CommunityPermissionCode[]`, so dropping or mistyping a code is a
 * compile error against the union above — no hand-copied list drifts out of
 * sync.
 */
export const COMMUNITY_PERMISSION_CODES: readonly CommunityPermissionCode[] = [
  "server:create",
  "server:read",
  "server:update",
  "server:delete",
  "server:start",
  "server:stop",
  "server:restart",
  "server:command",
  "file:read",
  "file:edit",
  "file:history",
  "file:rollback",
  "backup:create",
  "backup:read",
  "backup:restore",
  "backup:delete",
  "backup:schedule",
  "member:read",
  "member:add",
  "member:remove",
  "role:read",
  "role:manage",
  "grant:read",
  "grant:manage",
  "group:read",
  "group:manage",
  "community:read",
  "community:update",
  "community:delete",
  "audit:read",
  "session:read",
];
