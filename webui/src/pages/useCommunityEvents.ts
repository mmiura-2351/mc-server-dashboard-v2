/**
 * Live community status for the dashboard (WEBUI_SPEC.md 6.2, 7.2).
 *
 * Wires the framework-free {@link CommunityEventsClient} into the dashboard:
 * STATUS frames patch the per-community servers-list query cache in place
 * (`setQueryData`) so a card's pill updates without a refetch; a frame for a
 * server not in the loaded list (created after load) triggers one list refetch
 * to pick it up. While the socket is down it falls back to polling the servers
 * list every 10s (status only) and reports `degraded` so the dashboard can show
 * the live-degraded indicator; healthy WS does no polling. Because the API
 * replays nothing on subscribe, every reconnect and every GAP frame (dropped
 * frames on a slow client) triggers one list refetch to reconcile whatever the
 * missed window contained (#1723).
 *
 * The client is recreated per active community id and torn down on switch /
 * unmount (sign-out unmounts the dashboard), so a stale community's socket
 * never patches another community's cache.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { components } from "../api/schema";
import { useToast } from "../components/Toast.tsx";
import {
  CommunityEventsClient,
  type NotificationEvent,
  type StatusEvent,
} from "./communityEvents.ts";

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
  const { showToast } = useToast();
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

    // Resync gate (#1723): true only until the socket's first connect outcome.
    // Any later open — drop→reopen, an open after failed initial connects, or
    // a rotation reconnect — follows a window in which status frames may have
    // been dropped and are never replayed, so it must refetch the list once.
    // The pristine first open needs no refetch: the mount fetch covers it.
    let pristine = true;

    // An operator notice (today only `schedule_failed`, #1838) is a failure
    // toast: the payload's title/detail are the human-readable message the API
    // already localized to English, surfaced verbatim like an API error string.
    const notify = (event: NotificationEvent) => {
      const message =
        event.detail !== "" ? `${event.title} — ${event.detail}` : event.title;
      showToast(message, "error");
    };

    const client = new CommunityEventsClient(communityId, {
      onStatus: applyStatus,
      onNotification: notify,
      onGap: () => {
        // The stream fell behind and dropped status frames for an unknown set
        // of servers: one list refetch reconciles them (#1723).
        queryClient.invalidateQueries({ queryKey: serversKey(communityId) });
      },
      onOpen: () => {
        stopPolling();
        if (!pristine) {
          // Transitions between the last poll tick (or the drop itself) and
          // this reopen were lost for good; reconcile once (#1723).
          queryClient.invalidateQueries({ queryKey: serversKey(communityId) });
        }
        pristine = false;
        setDegraded(false);
      },
      onDown: () => {
        // The socket reports onDown on the first failure and every later drop;
        // each one re-enters degraded and (re)arms the poll.
        pristine = false;
        setDegraded(true);
        startPolling();
      },
    });
    client.start();

    return () => {
      client.close();
      stopPolling();
    };
  }, [communityId, queryClient, showToast]);

  return degraded;
}
