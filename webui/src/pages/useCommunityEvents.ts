/**
 * Live community status for the dashboard (WEBUI_SPEC.md 6.2, 7.2).
 *
 * Wires the framework-free {@link CommunityEventsClient} into the dashboard:
 * STATUS frames patch the per-community servers-list query cache in place
 * (`setQueryData`) so a card's pill updates without a refetch; a frame for a
 * server not in the loaded list (created after load) triggers one list refetch
 * to pick it up. While the socket is down it falls back to polling the servers
 * list every 10s (status only) and reports `degraded` so the dashboard can show
 * the live-degraded indicator; healthy WS does no polling.
 *
 * The client is recreated per active community id and torn down on switch /
 * unmount (sign-out unmounts the dashboard), so a stale community's socket
 * never patches another community's cache.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { components } from "../api/schema";
import { CommunityEventsClient, type StatusEvent } from "./communityEvents.ts";

type ServerResponse = components["schemas"]["ServerResponse"];

/** Poll interval while degraded (WEBUI_SPEC.md 6.2: 10s status-only polling). */
const POLL_INTERVAL_MS = 10000;

/** The community-scoped servers-list query key (shared with DashboardPage). */
export function serversKey(communityId: string) {
  return ["communities", communityId, "servers"] as const;
}

/**
 * Subscribe to the community events stream for `communityId`. Returns whether
 * the dashboard is in degraded (polling) mode.
 */
export function useCommunityEvents(communityId: string): boolean {
  const queryClient = useQueryClient();
  const [degraded, setDegraded] = useState(false);

  useEffect(() => {
    setDegraded(false);
    let pollTimer: ReturnType<typeof setInterval> | null = null;

    const stopPolling = () => {
      if (pollTimer !== null) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    // Status-only fallback while the WS is down: refetch the list every 10s.
    const startPolling = () => {
      if (pollTimer !== null) {
        return;
      }
      pollTimer = setInterval(() => {
        queryClient.invalidateQueries({ queryKey: serversKey(communityId) });
      }, POLL_INTERVAL_MS);
    };

    const applyStatus = (event: StatusEvent) => {
      const key = serversKey(communityId);
      const current = queryClient.getQueryData<ServerResponse[]>(key);
      if (current === undefined) {
        return;
      }
      const found = current.some((s) => s.id === event.serverId);
      if (!found) {
        // A server created after the list loaded: one refetch picks it up.
        queryClient.invalidateQueries({ queryKey: key });
        return;
      }
      queryClient.setQueryData<ServerResponse[]>(key, (servers) =>
        servers?.map((s) =>
          s.id === event.serverId ? { ...s, observed_state: event.state } : s,
        ),
      );
    };

    const client = new CommunityEventsClient(communityId, {
      onStatus: applyStatus,
      onOpen: () => {
        stopPolling();
        setDegraded(false);
      },
      onDown: () => {
        // The socket reports onDown on the first failure and every later drop;
        // each one re-enters degraded and (re)arms the poll.
        setDegraded(true);
        startPolling();
      },
    });
    client.start();

    return () => {
      client.close();
      stopPolling();
    };
  }, [communityId, queryClient]);

  return degraded;
}
