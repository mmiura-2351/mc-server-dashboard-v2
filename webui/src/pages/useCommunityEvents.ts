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
 * response lands unless that read's request started after it
 * ({@link observeRestResponses}), so an older response never rolls a pill back.
 * The server set is reconciled the same way: a live server missing from the
 * landed list, or a listed server missing from a newer snapshot, refetches it.
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
import { observeRestResponses, stamp } from "./restReads.ts";

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
    // The latest snapshot's server set, until a REST read supersedes it.
    let members: { ids: Set<string>; at: number } | null = null;

    const refetch = () => {
      queryClient.invalidateQueries({ queryKey: key });
    };

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

    // Apply the live states to the cached list and reconcile its server set:
    // a live server it lacks was created after its read, a listed server a
    // newer snapshot lacks was deleted since — either way one refetch loads
    // the current set. Deferred until the list has loaded.
    const reconcile = () => {
      const current = queryClient.getQueryData<ServerResponse[]>(key);
      if (current === undefined) {
        return;
      }
      patch();
      const listed = new Set(current.map((s) => s.id));
      const created = [...live.keys()].some((id) => !listed.has(id));
      const deleted =
        members !== null && current.some((s) => !members?.ids.has(s.id));
      if (created || deleted) {
        refetch();
      }
    };

    const unobserve = observeRestResponses(
      queryClient,
      key,
      (readStartedAt) => {
        for (const [id, entry] of live) {
          if (entry.at < readStartedAt) {
            live.delete(id);
          }
        }
        if (members !== null && members.at < readStartedAt) {
          members = null;
        }
        reconcile();
      },
    );

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
      pollTimer = setInterval(refetch, POLL_INTERVAL_MS);
    };

    const applyStatus = (event: StatusEvent) => {
      live.set(event.serverId, { state: event.state, at: stamp() });
      reconcile();
    };

    // The snapshot is every server's current state and the community's whole
    // server set (#1795): it replaces the live states and the membership.
    const applySnapshot = (servers: StatusEvent[]) => {
      const at = stamp();
      live.clear();
      for (const s of servers) {
        live.set(s.serverId, { state: s.state, at });
      }
      members = { ids: new Set(live.keys()), at };
      reconcile();
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
        refetch();
      },
      onOpen: () => {
        stopPolling();
        if (!pristine) {
          // Transitions between the last poll tick (or the drop itself) and
          // this reopen were lost for good; reconcile once (#1723).
          refetch();
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
      unobserve();
    };
  }, [communityId, queryClient, showToast]);

  return degraded;
}
