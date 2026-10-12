/** Share one socket across detail tabs; keep logs in an external store and metrics in bounded state.
 * Mark lost logs at gaps/drops and reconcile status after reconnects because the API does not replay frames. */

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import type { components } from "../api/schema";
import { stripMinecraftCodes } from "./mcFormat.ts";
import { observeRestResponses, stamp } from "./restReads.ts";
import { ServerEventsClient, type ServerFrame } from "./serverEvents.ts";
import { serverKey } from "./serverKey.ts";
import { atRest, normalizeState } from "./serverState.ts";

type ServerResponse = components["schemas"]["ServerResponse"];

/** Compute stripped Minecraft text once when appending, keeping formatting work out of render paths. */
export type LogEntry =
  | {
      id: number;
      kind: "line";
      line: string;
      stripped: string;
      stream: "stdout" | "stderr";
    }
  | { id: number; kind: "gap" }
  | { id: number; kind: "command"; line: string; stripped: string }
  | { id: number; kind: "output"; line: string; stripped: string };

export interface MetricsSample {
  cpuMillis: number;
  memoryBytes: number;
  playerCount: number;
}

export type LocalEcho = { kind: "command" | "output"; line: string };

/** Bound logs and metrics so a long-lived detail page cannot grow without limit. */
const LOG_BUFFER_MAX = 2000;
export const TAIL_LINES = 200;
export const METRICS_WINDOW = 60;

function trim(entries: LogEntry[]): LogEntry[] {
  return entries.length > LOG_BUFFER_MAX
    ? entries.slice(-LOG_BUFFER_MAX)
    : entries;
}

/** Notify log subscribers without updating the parent; only components using useLogs rerender on new lines. */
export interface LogStore {
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => LogEntry[];
  appendLine: (line: string, stream: "stdout" | "stderr") => void;
  appendGap: () => void;
  appendLocal: (items: LocalEcho[]) => void;
  clear: () => void;
}

function createLogStore(): LogStore {
  let entries: LogEntry[] = [];
  let nextId = 0;
  const listeners = new Set<() => void>();

  function notify() {
    for (const fn of listeners) fn();
  }

  return {
    subscribe(fn: () => void) {
      listeners.add(fn);
      return () => {
        listeners.delete(fn);
      };
    },
    getSnapshot() {
      return entries;
    },
    appendLine(line: string, stream: "stdout" | "stderr") {
      entries = trim([
        ...entries,
        {
          id: nextId++,
          kind: "line" as const,
          line,
          stripped: stripMinecraftCodes(line),
          stream,
        },
      ]);
      notify();
    },
    appendGap() {
      entries = trim([...entries, { id: nextId++, kind: "gap" as const }]);
      notify();
    },
    appendLocal(items: LocalEcho[]) {
      entries = trim([
        ...entries,
        ...items.map((e) => ({
          id: nextId++,
          ...e,
          stripped: stripMinecraftCodes(e.line),
        })),
      ]);
      notify();
    },
    clear() {
      entries = [];
      notify();
    },
  };
}

export function useLogs(store: LogStore): LogEntry[] {
  return useSyncExternalStore(store.subscribe, store.getSnapshot);
}

export interface ServerEventsState {
  logStore: LogStore;
  metrics: MetricsSample[];
  degraded: boolean;
  statusDetail: string;
  /** Keep the crash reason and detail together through state transitions. */
  statusReason: string;
  appendLocal: (entries: LocalEcho[]) => void;
}

export function useServerEvents(
  communityId: string,
  serverId: string,
): ServerEventsState {
  const queryClient = useQueryClient();
  // Keep the log store stable so appended lines rerender only its subscribers.
  const [logStore] = useState(createLogStore);
  const [metrics, setMetrics] = useState<MetricsSample[]>([]);
  const [degraded, setDegraded] = useState(false);
  const [statusDetail, setStatusDetail] = useState("");
  const [statusReason, setStatusReason] = useState("");

  const appendLocal = useCallback(
    (entries: LocalEcho[]) => {
      logStore.appendLocal(entries);
    },
    [logStore],
  );

  useEffect(() => {
    logStore.clear();
    setMetrics([]);
    setDegraded(false);
    setStatusDetail("");
    setStatusReason("");
    const key = serverKey(communityId, serverId);

    // Retain the live state and receipt stamp until a newer REST attempt supersedes it.
    let live: { state: string; at: number } | null = null;

    const patch = () => {
      if (live === null) {
        return;
      }
      const { state } = live;
      queryClient.setQueryData<ServerResponse>(key, (current) =>
        current === undefined ? current : { ...current, observed_state: state },
      );
    };

    const refetch = () => {
      queryClient.invalidateQueries({ queryKey: key });
    };

    const unobserve = observeRestResponses(
      queryClient,
      key,
      (readStartedAt) => {
        if (live !== null && live.at < readStartedAt) {
          live = null;
        }
        patch();
      },
    );

    const applyState = (state: string) => {
      live = { state, at: stamp() };
      patch();
      // Clear metrics at rest because no stream will refresh them; show idle content rather than frozen values.
      if (atRest(normalizeState(state))) {
        setMetrics([]);
      }
    };

    // Snapshots lack persisted crash detail; retain it only while the snapshot reports the same state.
    let detailState: string | null = null;

    const onFrame = (frame: ServerFrame) => {
      if (frame.kind === "status") {
        applyState(frame.state);
        setStatusDetail(frame.detail);
        setStatusReason(frame.reason);
        detailState = frame.state;
        return;
      }
      if (frame.kind === "snapshot") {
        applyState(frame.state);
        if (frame.state !== detailState) {
          setStatusDetail("");
          setStatusReason("");
          detailState = frame.state;
        }
        return;
      }
      if (frame.kind === "metrics") {
        setMetrics((prev) =>
          [
            ...prev,
            {
              cpuMillis: frame.cpuMillis,
              memoryBytes: frame.memoryBytes,
              playerCount: frame.playerCount,
            },
          ].slice(-METRICS_WINDOW),
        );
        return;
      }
      if (frame.kind === "log") {
        logStore.appendLine(frame.line, frame.stream);
        return;
      }
      // A gap means lost frames: add a log marker and refetch detail once to recover status.
      logStore.appendGap();
      refetch();
    };

    // Mark an open socket's drop once; failed reconnect attempts must not add duplicate gap markers.
    let wasOpen = false;
    // Refetch after every later open, including token rotation; status changes during the disconnect are not replayed.
    let pristine = true;

    const client = new ServerEventsClient(communityId, serverId, {
      onFrame,
      onOpen: () => {
        if (!pristine) {
          refetch();
        }
        pristine = false;
        wasOpen = true;
        setDegraded(false);
      },
      onDown: () => {
        pristine = false;
        if (wasOpen) {
          wasOpen = false;
          logStore.appendGap();
        }
        setDegraded(true);
        refetch();
      },
    });
    client.start();

    return () => {
      client.close();
      unobserve();
    };
  }, [communityId, serverId, queryClient, logStore]);

  return {
    logStore,
    metrics,
    degraded,
    statusDetail,
    statusReason,
    appendLocal,
  };
}
