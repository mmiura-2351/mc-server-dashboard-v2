/** Community-wide codes always apply; resource grants apply only when the check names the matching server. */

import type { components } from "../api/schema";
import type { PermissionCode } from "./catalog.ts";

export type EffectivePermissions =
  components["schemas"]["EffectivePermissionsResponse"];

export interface ResourceRef {
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
