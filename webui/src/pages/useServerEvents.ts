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
 * polling fallback — those streams resume when the socket reopens. Because the
 * API replays nothing on subscribe, every reconnect and every gap frame also
 * refetches the detail query once, so a status transition from the missed
 * window cannot leave the header pill stale forever (#1723).
 *
 * The client is recreated per (community, server) pair and torn down on unmount
 * / navigation, so a stale page's socket never patches another server's cache.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import type { components } from "../api/schema";
import { ServerEventsClient, type ServerFrame } from "./serverEvents.ts";
import { serverKey } from "./serverKey.ts";
import { atRest, normalizeState } from "./serverState.ts";

type ServerResponse = components["schemas"]["ServerResponse"];

/** A log buffer entry: a parsed line, a gap divider, or a local RCON echo. */
export type LogEntry =
  | { id: number; kind: "line"; line: string; stream: "stdout" | "stderr" }
  | { id: number; kind: "gap" }
  // A locally-echoed RCON command and its output (WEBUI_SPEC.md 6.5).
  | { id: number; kind: "command"; line: string }
  | { id: number; kind: "output"; line: string };

/** A windowed metrics sample for the sparklines. */
export interface MetricsSample {
  cpuMillis: number;
  memoryBytes: number;
  playerCount: number;
}

/** A local RCON echo to append into the stream (command + its output). */
export type LocalEcho = { kind: "command" | "output"; line: string };

/** The live view a detail page exposes to its tabs. */
export interface ServerEventsState {
  logs: LogEntry[];
  metrics: MetricsSample[];
  degraded: boolean;
  /** The detail string from the latest status frame (crash reason, etc.). */
  statusDetail: string;
  /** Append locally-echoed RCON lines (command + output) into the stream. */
  appendLocal: (entries: LocalEcho[]) => void;
}

/**
 * Caps. The full log buffer is bounded so a long-lived page does not grow
 * without limit; the Overview tail shows the last {@link TAIL_LINES} of it. The
 * metrics window is the last N samples kept client-side only (no persistence,
 * SPEC Section 8 / 6.4).
 */
const LOG_BUFFER_MAX = 2000;
export const TAIL_LINES = 200;
export const METRICS_WINDOW = 60;

/**
 * Subscribe to the server events stream for `(communityId, serverId)`. Returns
 * the live log/metrics buffers, the degraded flag and a local-echo appender.
 */
export function useServerEvents(
  communityId: string,
  serverId: string,
): ServerEventsState {
  const queryClient = useQueryClient();
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [metrics, setMetrics] = useState<MetricsSample[]>([]);
  const [degraded, setDegraded] = useState(false);
  const [statusDetail, setStatusDetail] = useState("");
  // A monotonic id so React keys stay stable across the bounded-buffer trim.
  const nextId = useRef(0);

  const appendLocal = useCallback((entries: LocalEcho[]) => {
    setLogs((prev) =>
      trim([...prev, ...entries.map((e) => ({ id: nextId.current++, ...e }))]),
    );
  }, []);

  useEffect(() => {
    setLogs([]);
    setMetrics([]);
    setDegraded(false);
    setStatusDetail("");

    const appendGap = () =>
      setLogs((prev) => trim([...prev, { id: nextId.current++, kind: "gap" }]));

    const onFrame = (frame: ServerFrame) => {
      if (frame.kind === "status") {
        // Patch the detail query so the header pill updates live (no refetch).
        queryClient.setQueryData<ServerResponse>(
          serverKey(communityId, serverId),
          (current) =>
            current === undefined
              ? current
              : { ...current, observed_state: frame.state },
        );
        setStatusDetail(frame.detail);
        // Once the server settles at rest there is no metrics stream (SPEC
        // 7.2); drop the windowed samples so the strip falls back to the idle
        // copy instead of freezing the last numbers forever.
        if (atRest(normalizeState(frame.state))) {
          setMetrics([]);
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
        setLogs((prev) =>
          trim([
            ...prev,
            {
              id: nextId.current++,
              kind: "line",
              line: frame.line,
              stream: frame.stream,
            },
          ]),
        );
        return;
      }
      // gap: the stream dropped frames (slow-client overflow). Mark the log
      // buffer and refetch the detail query once — a dropped status frame is
      // never replayed (#1723).
      appendGap();
      queryClient.invalidateQueries({
        queryKey: serverKey(communityId, serverId),
      });
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
          queryClient.invalidateQueries({
            queryKey: serverKey(communityId, serverId),
          });
        }
        pristine = false;
        wasOpen = true;
        setDegraded(false);
      },
      onDown: () => {
        pristine = false;
        if (wasOpen) {
          wasOpen = false;
          appendGap();
        }
        setDegraded(true);
        // Status-only REST fallback: one refetch picks up the latest observed
        // state while the socket is down (no log/metrics polling, SPEC 7.2).
        queryClient.invalidateQueries({
          queryKey: serverKey(communityId, serverId),
        });
      },
    });
    client.start();

    return () => client.close();
  }, [communityId, serverId, queryClient]);

  return { logs, metrics, degraded, statusDetail, appendLocal };
}

/** Keep only the most recent {@link LOG_BUFFER_MAX} log entries. */
function trim(entries: LogEntry[]): LogEntry[] {
  return entries.length > LOG_BUFFER_MAX
    ? entries.slice(-LOG_BUFFER_MAX)
    : entries;
}
