/**
 * Permission code catalog (WEBUI_SPEC.md 2.2).
 *
 * The community axis is the 30-code set assignable to roles / grants. The
 * platform axis is flag-driven (not assignable to roles) but is still part of
 * the caller's effective set the API returns, so it is included in the union
 * so `can()` can be typed against every code the UI may check.
 *
 * Codes are typed as a string-literal union rather than bare `string` so a
 * typo in a `can()` call fails typecheck.
 */

/** Community-axis codes (30) — the role/grant editor's source of truth. */
export type CommunityPermissionCode =
  | "server:create"
  | "server:read"
  | "server:update"
  | "server:delete"
  | "server:start"
  | "server:stop"
  | "server:restart"
  | "server:command"
  | "file:read"
  | "file:edit"
  | "file:history"
  | "file:rollback"
  | "backup:create"
  | "backup:read"
  | "backup:restore"
  | "backup:delete"
  | "backup:schedule"
  | "member:read"
  | "member:add"
  | "member:remove"
  | "role:read"
  | "role:manage"
  | "grant:read"
  | "grant:manage"
  | "group:read"
  | "group:manage"
  | "community:read"
  | "community:update"
  | "community:delete"
  | "audit:read";

/** Platform-axis codes (flag-driven, not assignable to roles). */
export type PlatformPermissionCode =
  | "worker:manage"
  | "community:provision"
  | "platform:monitor";

/** Every permission code the UI may check `can()` against. */
export type PermissionCode = CommunityPermissionCode | PlatformPermissionCode;
