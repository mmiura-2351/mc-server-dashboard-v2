/**
 * The optimistic start/stop/restart mutation shared by the dashboard rows and
 * the server-detail controls (issue #3146).
 *
 * One state machine for both surfaces, applied to the server `serverId` in the
 * `cacheKey` entry — the dashboard's community list or the detail page's single
 * server:
 *
 * - on request, its `observed_state` becomes the transition the verb asks for,
 *   so the pill moves before the API answers (#1071);
 * - on failure, only that server's `observed_state` is rolled back, and only
 *   while the cache still holds the transitional value written here: a WS
 *   status frame that landed mid-flight is newer than the captured state and
 *   takes precedence (#1727);
 * - once settled either way, `invalidateKeys` refetch, so the server's own
 *   answer replaces whatever the cache holds.
 *
 * What a failure means to the operator (the 403 glue, the EULA prompt, the
 * toast) stays with each surface through `onError`.
 */

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
  // Query string for the verb's POST, without the `?` (e.g. `force=true`).
  query?: string;
}

/** The transitional state a lifecycle verb requests. */
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
