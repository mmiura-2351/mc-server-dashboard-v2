/** Deny while capabilities load or no community is active; UI state cannot grant authority. */

import { useQuery } from "@tanstack/react-query";
import { useCallback } from "react";
import { useSession } from "../auth/SessionProvider.tsx";
import { useActiveCommunity } from "./ActiveCommunityProvider.tsx";
import { capabilitiesKey, fetchCapabilities } from "./capabilities.ts";
import type { PermissionCode } from "./catalog.ts";
import {
  type EffectivePermissions,
  type ResourceRef,
  resolvePermission,
} from "./resolve.ts";

const EMPTY: EffectivePermissions = { permissions: [], grants: [] };

function useCapabilities(): EffectivePermissions {
  const { status } = useSession();
  const { communityId } = useActiveCommunity();
  const { data } = useQuery({
    queryKey: capabilitiesKey(communityId ?? ""),
    queryFn: ({ signal }) => fetchCapabilities(communityId as string, signal),
    enabled: status === "signed-in" && communityId !== null,
  });
  return data ?? EMPTY;
}

export type Can = (code: PermissionCode, resource?: ResourceRef) => boolean;

export function useCan(): Can {
  const capabilities = useCapabilities();
  return useCallback(
    (code: PermissionCode, resource?: ResourceRef) =>
      resolvePermission(capabilities, code, resource),
    [capabilities],
  );
}

export function useCanCode(
  code: PermissionCode,
  resource?: ResourceRef,
): boolean {
  const capabilities = useCapabilities();
  return resolvePermission(capabilities, code, resource);
}
