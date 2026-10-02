/**
 * Live community status for the dashboard (WEBUI_SPEC.md 6.2, 7.2).
 *
 * Wires the framework-free {@link CommunityEventsClient} into the dashboard:
 * STATUS frames patch the per-community servers-list query cache in place
 * (`setQueryData`) so a card's pill updates without a refetch; a frame for a
 * server not in the loaded list (created after load) triggers one list refetch
 * to pick it up. While the socket is down it falls back to polling the servers
 * list every 10s (status only) and reports `degraded` so the dashboard can show
 * the live-degraded indicator; healthy WS does no polling. The API replays no
 * missed frames, but it opens every connection — and follows every GAP frame
 * (dropped frames on a slow client) — with a status snapshot of the whole
 * community, which patches the cache the same way (#1795). Every reconnect and
 * every GAP frame still triggers one list refetch as a belt-and-suspenders
 * reconcile (#1723).
 *
 * A live state outlives the list's REST reads (#3213): one received before the
 * list has loaded, or while a read is in flight, is re-applied when the
 * response lands unless that read started after it ({@link observeRestReads}),
 * so an older response never rolls a pill back.
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
import { observeRestReads } from "./restReads.ts";

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
    const key = serversKey(communityId);
    let pollTimer: ReturnType<typeof setInterval> | null = null;

    // The live states not yet superseded by a REST read, each stamped with
    // when it was received (#3213).
    const live = new Map<string, { state: string; at: number }>();

    // Write every live state over the cached list.
    const patch = () => {
      if (live.size === 0) {
        return;
      }
      queryClient.setQueryData<ServerResponse[]>(key, (servers) =>
        servers?.map((s) => {
          const entry = live.get(s.id);
          return entry === undefined
            ? s
            : { ...s, observed_state: entry.state };
        }),
      );
    };

    const reads = observeRestReads(queryClient, key, (readStartedAt) => {
      for (const [id, entry] of live) {
        if (entry.at < readStartedAt) {
          live.delete(id);
        }
      }
      patch();
    });

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
      pollTimer = setInterval(reads.refetch, POLL_INTERVAL_MS);
    };

    const applyStatus = (event: StatusEvent) => {
      live.set(event.serverId, { state: event.state, at: reads.stamp() });
      const current = queryClient.getQueryData<ServerResponse[]>(key);
      if (current === undefined) {
        // The list has not loaded: the state is applied when it lands.
        return;
      }
      if (!current.some((s) => s.id === event.serverId)) {
        // A server created after the list loaded: one refetch picks it up.
        reads.refetch();
        return;
      }
      patch();
    };

    // The snapshot is every server's current state (#1795): patch them all in
    // place. It carries states only, so a server created or deleted since the
    // list loaded (the sets differ) needs one list refetch.
    const applySnapshot = (servers: StatusEvent[]) => {
      const at = reads.stamp();
      live.clear();
      for (const s of servers) {
        live.set(s.serverId, { state: s.state, at });
      }
      const current = queryClient.getQueryData<ServerResponse[]>(key);
      if (current === undefined) {
        // The list has not loaded: the states are applied when it lands.
        return;
      }
      patch();
      if (
        live.size !== current.length ||
        current.some((s) => !live.has(s.id))
      ) {
        reads.refetch();
      }
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
      onSnapshot: applySnapshot,
      onNotification: notify,
      onGap: () => {
        // The stream fell behind and dropped status frames for an unknown set
        // of servers: one list refetch reconciles them (#1723).
        reads.refetch();
      },
      onOpen: () => {
        stopPolling();
        if (!pristine) {
          // Transitions between the last poll tick (or the drop itself) and
          // this reopen were lost for good; reconcile once (#1723).
          reads.refetch();
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
      reads.close();
    };
  }, [communityId, queryClient, showToast]);

  return degraded;
}
