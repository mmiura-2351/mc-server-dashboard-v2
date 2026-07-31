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
  /** True while a communities fetch is in flight (initial or background). */
  communitiesFetching: boolean;
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
    isSuccess: communitiesLoaded,
    isError: communitiesError,
    isFetching: communitiesFetching,
    refetch: refetchCommunities,
  } = useCommunities(signedIn);

  // null = no explicit selection yet; we derive the default from the community
  // list at render time. An explicit setCommunityId(...) wins over the default.
  const [selected, setSelected] = useState<string | null>(null);

  const setCommunityId = useCallback((id: string | null) => {
    setSelected(id);
  }, []);

  // Derive the effective community id at render time. When the user has
  // explicitly selected a community, verify it still exists in the list; if it
  // vanished (removed by an owner, or community deleted) fall back to the first
  // community (or null when the list is empty). This also handles the
  // self-delete flow where setCommunityId(null) is called while the user still
  // belongs to other communities (issue #2015).
  const communityId =
    communities !== undefined &&
    selected !== null &&
    communities.some((c) => c.id === selected)
      ? selected
      : (communities?.[0]?.id ?? null);

  // Drop a selection the server has confirmed gone, so it cannot resurrect if
  // the community reappears in a later list (issue #2164). "Confirmed gone"
  // means the query settled successfully on a non-empty list that lacks the
  // selection: a pending or failed fetch says nothing about the selection's
  // fate, and an empty list is equally uninformative (it already renders as no
  // active community, and dropping the selection there would discard a valid
  // choice on a transiently empty response).
  useEffect(() => {
    if (
      communitiesLoaded &&
      selected !== null &&
      communities !== undefined &&
      communities.length > 0 &&
      !communities.some((c) => c.id === selected)
    ) {
      setSelected(null);
    }
  }, [communitiesLoaded, communities, selected]);

  // Dropping out of the signed-in state clears the selection so a later
  // sign-in re-derives the default rather than reusing a stale id.
  useEffect(() => {
    if (!signedIn) {
      setSelected(null);
    }
  }, [signedIn]);

  const value = useMemo<ActiveCommunityValue>(
    () => ({
      communityId,
      setCommunityId,
      communities,
      communitiesError,
      communitiesFetching,
      refetchCommunities: () => {
        refetchCommunities();
      },
    }),
    [
      communityId,
      setCommunityId,
      communities,
      communitiesError,
      communitiesFetching,
      refetchCommunities,
    ],
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
