/**
 * Active community state (WEBUI_SPEC.md 7.3).
 *
 * A minimal context holding the "current community id" plus a setter. The real
 * switcher UI is Phase 3; for now the state defaults to the first community
 * from `GET /communities` once signed in, and exposes the setter so a switcher
 * can be wired later without touching this module.
 *
 * A user with no communities resolves to `null`, which downstream hooks treat
 * as "no active community" (no permissions fetched).
 */

import { useQuery } from "@tanstack/react-query";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api } from "../api/client.ts";
import type { components } from "../api/schema";
import { useSession } from "../auth/SessionProvider.tsx";

type Community = components["schemas"]["CommunityResponse"];

interface ActiveCommunityValue {
  /** The active community id, or null when none is selected / available. */
  communityId: string | null;
  setCommunityId: (id: string | null) => void;
  /**
   * The caller's communities, or undefined while still loading. Shared with the
   * top-bar switcher so it reuses the same query instead of re-fetching.
   */
  communities: Community[] | undefined;
  /** True when fetching communities has failed (all retries exhausted). */
  communitiesError: boolean;
  /** Re-fetch the communities list (e.g. after an error). */
  refetchCommunities: () => void;
}

const ActiveCommunityContext = createContext<ActiveCommunityValue | null>(null);

/** Communities the caller belongs to; the first is the default active one. */
function useCommunities(signedIn: boolean) {
  return useQuery({
    queryKey: ["communities"],
    queryFn: ({ signal }) => api.get("/api/communities", { signal }),
    enabled: signedIn,
  });
}

export function ActiveCommunityProvider({ children }: { children: ReactNode }) {
  const { status } = useSession();
  const signedIn = status === "signed-in";
  const {
    data: communities,
    isError: communitiesError,
    refetch: refetchCommunities,
  } = useCommunities(signedIn);

  // null = no explicit selection yet; we fall back to the first community once
  // the list arrives. An explicit setCommunityId(...) wins over the default.
  const [selected, setSelected] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);

  const setCommunityId = useCallback((id: string | null) => {
    setTouched(true);
    setSelected(id);
  }, []);

  // Default to the first community once the list loads, unless the user (or a
  // future switcher) has already chosen one.
  useEffect(() => {
    if (!touched && communities !== undefined) {
      setSelected(communities.length > 0 ? communities[0].id : null);
    }
  }, [touched, communities]);

  // Dropping out of the signed-in state clears the selection so a later
  // sign-in re-derives the default rather than reusing a stale id.
  useEffect(() => {
    if (!signedIn) {
      setSelected(null);
      setTouched(false);
    }
  }, [signedIn]);

  const refetch = useCallback(() => {
    refetchCommunities();
  }, [refetchCommunities]);

  const value = useMemo<ActiveCommunityValue>(
    () => ({
      communityId: selected,
      setCommunityId,
      communities,
      communitiesError,
      refetchCommunities: refetch,
    }),
    [selected, setCommunityId, communities, communitiesError, refetch],
  );

  return (
    <ActiveCommunityContext.Provider value={value}>
      {children}
    </ActiveCommunityContext.Provider>
  );
}

export function useActiveCommunity(): ActiveCommunityValue {
  const value = useContext(ActiveCommunityContext);
  if (value === null) {
    throw new Error(
      "useActiveCommunity must be used within an ActiveCommunityProvider",
    );
  }
  return value;
}
