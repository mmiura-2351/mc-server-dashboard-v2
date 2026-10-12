/** Patch status frames into the dashboard cache; refetch when snapshots reveal a changed server set.
 * Poll only while disconnected and refetch after gaps/reconnects because missed frames are not replayed. */

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

const POLL_INTERVAL_MS = 10000;

export function serversKey(communityId: string) {
  return ["communities", communityId, "servers"] as const;
}

export function useCommunityEvents(communityId: string): boolean {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [degraded, setDegraded] = useState(false);

  useEffect(() => {
    setDegraded(false);
    const key = serversKey(communityId);
    let pollTimer: ReturnType<typeof setInterval> | null = null;

    // Retain each live state's receipt stamp until a newer REST attempt supersedes it.
    const live = new Map<string, { state: string; at: number }>();
    // Retain the snapshot's server set until a newer REST read supersedes it.
    let members: { ids: Set<string>; at: number } | null = null;

    const refetch = () => {
      queryClient.invalidateQueries({ queryKey: key });
    };

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

    // Reconcile missing or extra servers after the list loads; live snapshots can be newer than REST membership.
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

    const applySnapshot = (servers: StatusEvent[]) => {
      const at = stamp();
      live.clear();
      for (const s of servers) {
        live.set(s.serverId, { state: s.state, at });
      }
      members = { ids: new Set(live.keys()), at };
      reconcile();
    };

    // Refetch on every open after the first connection outcome; missed status frames are not replayed.
    // The pristine first open is covered by the mount request.
    let pristine = true;

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
        refetch();
      },
      onOpen: () => {
        stopPolling();
        if (!pristine) {
          refetch();
        }
        pristine = false;
        setDegraded(false);
      },
      onDown: () => {
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
