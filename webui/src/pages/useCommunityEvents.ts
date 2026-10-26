/**
 * Live community status for the dashboard (WEBUI_SPEC.md 6.2, 7.2).
 *
 * Wires the framework-free {@link CommunityEventsClient} into the dashboard:
 * STATUS frames patch the per-community servers-list query cache in place
 * (`setQueryData`) so a card's pill updates without a refetch, and so do its
 * actions when the server's intent changes elsewhere (#3428); a frame for a
 * server not in the loaded list (created after load) triggers one list refetch
 * to pick it up. While the socket is down it falls back to polling the servers
 * list every 10s (status only) and reports `degraded` so the dashboard can show
 * the live-degraded indicator; healthy WS does no polling. The API replays no
 * missed frames, but it opens every connection — and follows every GAP frame
 * that dropped status frames on a slow client — with a status snapshot of the
 * whole community, which patches the cache the same way (#1795). Every
 * reconnect and every GAP frame still triggers one list refetch as a
 * belt-and-suspenders reconcile (#1723).
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
  type SnapshotEntry,
  type StatusEvent,
} from "./communityEvents.ts";
import { observeRestResponses, stamp } from "./restReads.ts";

type ServerResponse = components["schemas"]["ServerResponse"];

/** A live state value, stamped with when it was received (#3213). */
type Stamped = { value: string; at: number };

/** The state fields a live write sets on a cached server. */
type Fields = Partial<Pick<ServerResponse, "observed_state" | "desired_state">>;

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

    // The live observed and desired states not yet superseded by a REST
    // read, each stamped with when it was received (#3213). The two are
    // stamped apart, as different frames carry them (#3428): a read can
    // supersede one and not the other.
    const observed = new Map<string, Stamped>();
    const desired = new Map<string, Stamped>();
    // The latest snapshot's server set, until a REST read supersedes it.
    let members: { ids: Set<string>; at: number } | null = null;

    const refetch = () => {
      queryClient.invalidateQueries({ queryKey: key });
    };

    // The fields of each server's live states, to write over a REST response.
    const live = (id: string): Fields | null => {
      const state = observed.get(id);
      const intent = desired.get(id);
      if (state === undefined && intent === undefined) {
        return null;
      }
      return {
        ...(state !== undefined && { observed_state: state.value }),
        ...(intent !== undefined && { desired_state: intent.value }),
      };
    };

    // Write `fields` over the cached list and reconcile its server set: a
    // live server it lacks was created after its read, a listed server a
    // newer snapshot lacks was deleted since — either way one refetch loads
    // the current set. Deferred until the list has loaded.
    //
    // A REST response or a snapshot replaces every state, so all live states
    // are written over it; a status frame writes only the fields it carries,
    // so it never rewrites a state another write (the optimistic transition
    // `useLifecycleMutation` puts in the cache, #3428) set since.
    const reconcile = (fields: (id: string) => Fields | null) => {
      const current = queryClient.getQueryData<ServerResponse[]>(key);
      if (current === undefined) {
        return;
      }
      if (current.some((s) => fields(s.id) !== null)) {
        queryClient.setQueryData<ServerResponse[]>(key, (servers) =>
          servers?.map((s) => {
            const update = fields(s.id);
            return update === null ? s : { ...s, ...update };
          }),
        );
      }
      const listed = new Set(current.map((s) => s.id));
      const created = [...observed.keys(), ...desired.keys()].some(
        (id) => !listed.has(id),
      );
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
        for (const live of [observed, desired]) {
          for (const [id, entry] of live) {
            if (entry.at < readStartedAt) {
              live.delete(id);
            }
          }
        }
        if (members !== null && members.at < readStartedAt) {
          members = null;
        }
        reconcile(live);
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

    // A status frame sets the states it carries: an observed transition, or
    // a change of the server's intent (#3428).
    const applyStatus = (event: StatusEvent) => {
      const at = stamp();
      if (event.state !== undefined) {
        observed.set(event.serverId, { value: event.state, at });
      }
      if (event.desiredState !== undefined) {
        desired.set(event.serverId, { value: event.desiredState, at });
      }
      const carried: Fields = {
        ...(event.state !== undefined && { observed_state: event.state }),
        ...(event.desiredState !== undefined && {
          desired_state: event.desiredState,
        }),
      };
      reconcile((id) => (id === event.serverId ? carried : null));
    };

    // The snapshot is every server's current states and the community's whole
    // server set (#1795): it replaces the live states and the membership.
    const applySnapshot = (servers: SnapshotEntry[]) => {
      const at = stamp();
      observed.clear();
      desired.clear();
      for (const s of servers) {
        observed.set(s.serverId, { value: s.state, at });
        if (s.desiredState !== undefined) {
          desired.set(s.serverId, { value: s.desiredState, at });
        }
      }
      members = { ids: new Set(observed.keys()), at };
      reconcile(live);
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
      onAccessLost: () => {
        // The user lost access to this community's servers (removed, the
        // community deleted, or `server:read` revoked) and the socket stopped
        // for good (#3313). Reload from REST so the page shows its own state:
        // the list is reset, not just refetched — a failed refetch keeps the
        // cached rows on screen — and the communities list drops one the
        // user is no longer in.
        stopPolling();
        queryClient.resetQueries({ queryKey: key });
        queryClient.invalidateQueries({
          queryKey: ["communities"],
          exact: true,
        });
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
