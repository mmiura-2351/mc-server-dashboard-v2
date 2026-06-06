/**
 * Permission hooks (WEBUI_SPEC.md 7.3).
 *
 * `useCan()` returns a `can(code, { serverId? })` resolver bound to the active
 * community's fetched permission set; `useCanCode()` is the component-friendly
 * single-check variant. Both deny while the set is loading or when there is no
 * active community — the UI never invents authority.
 */

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

/**
 * The active community's effective permission set, fetched once per community
 * and cached for the session. Returns the empty set until it loads or when no
 * community is active.
 */
function useCapabilities(): EffectivePermissions {
  const { status } = useSession();
  const { communityId } = useActiveCommunity();
  const { data } = useQuery({
    queryKey: capabilitiesKey(communityId ?? ""),
    queryFn: () => fetchCapabilities(communityId as string),
    enabled: status === "signed-in" && communityId !== null,
  });
  return data ?? EMPTY;
}

export type Can = (code: PermissionCode, resource?: ResourceRef) => boolean;

/** A resolver bound to the active community's permission set. */
export function useCan(): Can {
  const capabilities = useCapabilities();
  return useCallback(
    (code: PermissionCode, resource?: ResourceRef) =>
      resolvePermission(capabilities, code, resource),
    [capabilities],
  );
}

/** Single-check variant for components: `useCanCode("server:start")`. */
export function useCanCode(
  code: PermissionCode,
  resource?: ResourceRef,
): boolean {
  const capabilities = useCapabilities();
  return resolvePermission(capabilities, code, resource);
}
