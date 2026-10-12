/** Share optimistic lifecycle updates between list and detail caches.
 * Roll back only our transition; newer live status wins, and every settled mutation invalidates its cache. */

import {
  type QueryKey,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import type { LifecycleAction } from "./lifecycleErrors.ts";
import type { ObservedState } from "./serverState.ts";

type ServerResponse = components["schemas"]["ServerResponse"];
type ServerCache = ServerResponse | ServerResponse[];

export interface LifecycleRequest {
  action: LifecycleAction;
  // POST query string without the leading ?, e.g. force=true.
  query?: string;
}

export function transitionalState(action: LifecycleAction): ObservedState {
  if (action === "stop") {
    return "stopping";
  }
  if (action === "restart") {
    return "restarting";
  }
  return "starting";
}

function findServer(data: ServerCache | undefined, serverId: string) {
  return Array.isArray(data)
    ? data.find((s) => s.id === serverId)
    : data?.id === serverId
      ? data
      : undefined;
}

function updateServer(
  data: ServerCache | undefined,
  serverId: string,
  update: (server: ServerResponse) => ServerResponse,
): ServerCache | undefined {
  if (Array.isArray(data)) {
    return data.map((s) => (s.id === serverId ? update(s) : s));
  }
  return data?.id === serverId ? update(data) : data;
}

export function useLifecycleMutation({
  communityId,
  serverId,
  cacheKey,
  invalidateKeys,
  onError,
}: {
  communityId: string;
  serverId: string;
  cacheKey: QueryKey;
  invalidateKeys: readonly QueryKey[];
  onError: (error: unknown, action: LifecycleAction) => void;
}) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ action, query }: LifecycleRequest) => {
      const path = apiPath(
        `/api/communities/{community_id}/servers/{server_id}/${action}`,
        { community_id: communityId, server_id: serverId },
      );
      return query === undefined
        ? api.post(path)
        : api.post(`${path}?${query}` as never);
    },
    onMutate: ({ action }: LifecycleRequest) => {
      const previousState = findServer(
        queryClient.getQueryData<ServerCache>(cacheKey),
        serverId,
      )?.observed_state;
      const optimistic = transitionalState(action);
      queryClient.setQueryData<ServerCache>(cacheKey, (old) =>
        updateServer(old, serverId, (s) => ({
          ...s,
          observed_state: optimistic,
        })),
      );
      return { previousState, optimistic };
    },
    onError: (error, { action }, context) => {
      if (context?.previousState !== undefined) {
        const prev = context.previousState;
        queryClient.setQueryData<ServerCache>(cacheKey, (old) =>
          updateServer(old, serverId, (s) =>
            s.observed_state === context.optimistic
              ? { ...s, observed_state: prev }
              : s,
          ),
        );
      }
      onError(error, action);
    },
    onSettled: () => {
      for (const queryKey of invalidateKeys) {
        queryClient.invalidateQueries({ queryKey });
      }
    },
  });
}
