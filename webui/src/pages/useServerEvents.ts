/**
 * Live per-server events for the detail page (WEBUI_SPEC.md 6.4, 6.5, 7.2).
 *
 * Wires the framework-free {@link ServerEventsClient} into the detail page as
 * ONE socket shared by all tabs: it subscribes to status + log + metrics once,
 * accumulates a bounded log buffer and a windowed metrics buffer in React state,
 * and patches the server-detail query cache on each status frame so the header
 * pill updates live without a refetch. A `gap` frame is appended to the log
 * buffer as a marker so log views can render an inline "missed events" divider;
 * the hook appends the same marker itself when an open socket drops, because
 * lines emitted while it is down are never replayed (#1726).
 *
 * Degraded handling follows SPEC 7.2: on socket loss the client reconnects with
 * backoff and the hook refetches the detail query once (status-only REST
 * fallback) and reports `degraded` for the banner; there is NO log/metrics
 * polling fallback — those streams resume when the socket reopens. The API
 * replays no missed frames, but it opens every connection — and follows every
 * gap frame that dropped status — with a status snapshot, which patches the
 * header pill the same way a status frame does (#1795); every reconnect and
 * every gap frame also still refetches the detail query once as a
 * belt-and-suspenders reconcile (#1723). A live state outlives the detail query's REST reads (#3213): one
 * received before the query has loaded, or while a read is in flight, is
 * re-applied when the response lands unless that read's request started
 * after it ({@link observeRestResponses}), so an older response never rolls
 * the pill back.
 *
 * The client is recreated per (community, server) pair and torn down on unmount
 * / navigation, so a stale page's socket never patches another server's cache.
 *
 * Performance: the log buffer is managed as an external store (via
 * {@link LogStore}) so that appending a line does NOT trigger a React state
 * update in the parent component. Only tabs that subscribe via
 * {@link useLogs} re-render on log changes, keeping unrelated tabs (files,
 * settings, etc.) free from per-line re-renders (#1725).  Each entry carries
 * a pre-stripped `stripped` field so `stripMinecraftCodes` runs once at append
 * time rather than per entry per render frame.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import type { components } from "../api/schema";
import { stripMinecraftCodes } from "./mcFormat.ts";
import { observeRestResponses, stamp } from "./restReads.ts";
import { ServerEventsClient, type ServerFrame } from "./serverEvents.ts";
import { serverKey } from "./serverKey.ts";
import { atRest, normalizeState } from "./serverState.ts";

type ServerResponse = components["schemas"]["ServerResponse"];

/**
 * A log buffer entry: a parsed line, a gap divider, or a local RCON echo.
 *
 * Entries with a `line` also carry a `stripped` field — the line with
 * Minecraft §-formatting codes removed — computed once at append time so
 * render paths never need to call `stripMinecraftCodes` themselves (#1725).
 */
export type LogEntry =
  | {
      id: number;
      kind: "line";
      line: string;
      stripped: string;
      stream: "stdout" | "stderr";
    }
  | { id: number; kind: "gap" }
  // A locally-echoed RCON command and its output (WEBUI_SPEC.md 6.5).
  | { id: number; kind: "command"; line: string; stripped: string }
  | { id: number; kind: "output"; line: string; stripped: string };

/** A windowed metrics sample for the sparklines. */
export interface MetricsSample {
  cpuMillis: number;
  memoryBytes: number;
  playerCount: number;
}

/** A local RCON echo to append into the stream (command + its output). */
export type LocalEcho = { kind: "command" | "output"; line: string };

// ── Log store (external store for useSyncExternalStore) ────────────────────

/**
 * Caps. The full log buffer is bounded so a long-lived page does not grow
 * without limit; the Overview tail shows the last {@link TAIL_LINES} of it. The
 * metrics window is the last N samples kept client-side only (no persistence,
 * SPEC Section 8 / 6.4).
 */
const LOG_BUFFER_MAX = 2000;
export const TAIL_LINES = 200;
export const METRICS_WINDOW = 60;

/** Keep only the most recent {@link LOG_BUFFER_MAX} log entries. */
function trim(entries: LogEntry[]): LogEntry[] {
  return entries.length > LOG_BUFFER_MAX
    ? entries.slice(-LOG_BUFFER_MAX)
    : entries;
}

/**
 * An external store for the log buffer, designed for {@link useSyncExternalStore}.
 * Mutations notify subscribers without touching React state in the parent,
 * so only components that call {@link useLogs} re-render on log changes.
 */
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

/**
 * Subscribe to the log store and return the current entries array.
 * Only components that call this hook re-render on log changes (#1725).
 */
export function useLogs(store: LogStore): LogEntry[] {
  return useSyncExternalStore(store.subscribe, store.getSnapshot);
}

// ── Hook ───────────────────────────────────────────────────────────────────

/** The live view a detail page exposes to its tabs. */
export interface ServerEventsState {
  logStore: LogStore;
  metrics: MetricsSample[];
  degraded: boolean;
  /** The detail string from the latest status frame (crash reason, etc.). */
  statusDetail: string;
  /** Append locally-echoed RCON lines (command + output) into the stream. */
  appendLocal: (entries: LocalEcho[]) => void;
}

/**
 * Subscribe to the server events stream for `(communityId, serverId)`. Returns
 * the live log store, metrics buffer, the degraded flag and a local-echo
 * appender.
 */
export function useServerEvents(
  communityId: string,
  serverId: string,
): ServerEventsState {
  const queryClient = useQueryClient();
  // The log buffer lives in an external store (created once, never replaced)
  // so appending a line does NOT trigger a state update in this component —
  // only subscribers via useLogs re-render (#1725).
  const [logStore] = useState(createLogStore);
  const [metrics, setMetrics] = useState<MetricsSample[]>([]);
  const [degraded, setDegraded] = useState(false);
  const [statusDetail, setStatusDetail] = useState("");

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
    const key = serverKey(communityId, serverId);

    // The live state not yet superseded by a REST read, stamped with when it
    // was received (#3213).
    let live: { state: string; at: number } | null = null;

    // Patch the detail query so the header pill updates live (no refetch).
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
      // Once the server settles at rest there is no metrics stream (SPEC
      // 7.2); drop the windowed samples so the strip falls back to the idle
      // copy instead of freezing the last numbers forever.
      if (atRest(normalizeState(state))) {
        setMetrics([]);
      }
    };

    // The state whose live transition carried the current status detail. A
    // snapshot carries no detail (it is not persisted), so it keeps the detail
    // only while it still reports that same state (#1795).
    let detailState: string | null = null;

    const onFrame = (frame: ServerFrame) => {
      if (frame.kind === "status") {
        applyState(frame.state);
        setStatusDetail(frame.detail);
        detailState = frame.state;
        return;
      }
      if (frame.kind === "snapshot") {
        applyState(frame.state);
        if (frame.state !== detailState) {
          setStatusDetail("");
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
      // gap: the stream dropped frames (slow-client overflow). Mark the log
      // buffer and refetch the detail query once — a dropped status frame is
      // never replayed (#1723).
      logStore.appendGap();
      refetch();
    };

    // Lines emitted between a drop and the reconnect are lost for good (the
    // API replays nothing on subscribe), so mark the drop point with the same
    // gap marker. `wasOpen` gates it: no marker before the first open, and the
    // failed reconnect attempts that follow a drop (each fires onDown again)
    // do not stack markers.
    let wasOpen = false;
    // Resync gate (#1723): true only until the socket's first connect outcome.
    // Any later open — drop→reopen, an open after failed initial connects, or
    // a rotation reconnect (which `wasOpen` never sees: rotation tears down
    // without onDown) — follows a window in which status frames may have been
    // dropped, so it must refetch the detail query once. The pristine first
    // open needs no refetch: the mount fetch covers it.
    let pristine = true;

    const client = new ServerEventsClient(communityId, serverId, {
      onFrame,
      onOpen: () => {
        if (!pristine) {
          // The onDown fallback refetched at drop time; transitions between
          // that refetch and this reopen were lost for good, reconcile once.
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
        // Status-only REST fallback: one refetch picks up the latest observed
        // state while the socket is down (no log/metrics polling, SPEC 7.2).
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
    appendLocal,
  };
}
