/**
 * Pure permission resolution (WEBUI_SPEC.md 7.3).
 *
 * The effective set is `community-wide codes ∪ (matching per-resource grants)`.
 * A grant is server-scoped: its codes only answer a check that names the same
 * resource id. A check without a resource id is "community-wide" and a
 * resource-scoped grant must not satisfy it.
 */

import type { components } from "../api/schema";
import type { PermissionCode } from "./catalog.ts";

export type EffectivePermissions =
  components["schemas"]["EffectivePermissionsResponse"];

export interface ResourceRef {
  /** When set, the check is scoped to this server's grants. */
  serverId?: string;
}

export function resolvePermission(
  perms: EffectivePermissions,
  code: PermissionCode,
  resource?: ResourceRef,
): boolean {
  if (perms.permissions.includes(code)) {
    return true;
  }
  if (resource?.serverId === undefined) {
    return false;
  }
  return perms.grants.some(
    (grant) =>
      grant.resource_id === resource.serverId &&
      grant.permissions.includes(code),
  );
}
