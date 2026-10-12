/** Default to the first membership when no valid explicit selection exists; no communities means no capabilities. */

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
  communityId: string | null;
  setCommunityId: (id: string | null) => void;
  /** Share the membership query with the switcher; undefined means the initial list is unavailable. */
  communities: Community[] | undefined;
  communitiesError: boolean;
  communitiesFetching: boolean;
  refetchCommunities: () => void;
}

const ActiveCommunityContext = createContext<ActiveCommunityValue | null>(null);

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

  // A valid explicit selection wins; otherwise derive the default during render.
  const [selected, setSelected] = useState<string | null>(null);

  const setCommunityId = useCallback((id: string | null) => {
    setSelected(id);
  }, []);

  // Fall back to the first membership when the selected community disappears, or null when none remain.
  const communityId =
    communities !== undefined &&
    selected !== null &&
    communities.some((c) => c.id === selected)
      ? selected
      : (communities?.[0]?.id ?? null);

  // Clear a missing selection only after a successful nonempty list confirms its absence.
  // Pending, failed, and empty responses must not discard a potentially valid choice.
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
