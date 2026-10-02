import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import { act, render } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken, setAccessToken } from "../auth/tokenStore.ts";
import { installMockWebSocket, MockWebSocket } from "../test/mockWebSocket.ts";
import { serverKey } from "./serverKey.ts";
import type { LogEntry } from "./useServerEvents.ts";
import {
  METRICS_WINDOW,
  type ServerEventsState,
  useLogs,
  useServerEvents,
} from "./useServerEvents.ts";

const CID = "c1";
const SID = "s1";

function frame(stream: string, payload: unknown) {
  return JSON.stringify({ stream, ts: "t", payload });
}

let state: ServerEventsState;
let logs: LogEntry[];

function Probe() {
  state = useServerEvents(CID, SID);
  logs = useLogs(state.logStore);
  return null;
}

function setup() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  // Counts resync requests by call. (A cache-event spy would undercount here:
  // with no observer to refetch and clear `isInvalidated`, only the first
  // invalidation of a query emits an "invalidate" event.)
  const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const view = render(<Probe />, { wrapper });
  return { queryClient, invalidateSpy, view };
}

describe("useServerEvents", () => {
  let restore: () => void;

  beforeEach(() => {
    vi.useFakeTimers();
    restore = installMockWebSocket();
    setAccessToken("tok-1");
  });

  afterEach(() => {
    restore();
    clearAccessToken();
    vi.useRealTimers();
  });

  it("routes log frames into the buffer color-keyed by std stream", () => {
    setup();
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        frame("log", { line: "a", stream: "stdout" }),
      );
      MockWebSocket.last().message(
        frame("log", { line: "b", stream: "stderr" }),
      );
    });
    expect(logs).toEqual([
      {
        id: expect.any(Number),
        kind: "line",
        line: "a",
        stripped: "a",
        stream: "stdout",
      },
      {
        id: expect.any(Number),
        kind: "line",
        line: "b",
        stripped: "b",
        stream: "stderr",
      },
    ]);
  });

  it("strips Minecraft formatting codes once at append time", () => {
    setup();
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        frame("log", { line: "§aGreen §cRed", stream: "stdout" }),
      );
    });
    expect(logs).toEqual([
      {
        id: expect.any(Number),
        kind: "line",
        line: "§aGreen §cRed",
        stripped: "Green Red",
        stream: "stdout",
      },
    ]);
  });

  it("appends a gap marker into the log buffer", () => {
    setup();
    act(() => {
      MockWebSocket.last().message(frame("gap", {}));
    });
    expect(logs).toEqual([{ id: expect.any(Number), kind: "gap" }]);
  });

  it("does not append a gap marker on the initial open", () => {
    setup();
    act(() => {
      MockWebSocket.last().open();
    });
    expect(logs).toEqual([]);
  });

  it("does not append a gap marker when connects fail before any open", () => {
    setup();
    act(() => {
      MockWebSocket.last().fail();
    });
    act(() => {
      vi.advanceTimersByTime(30000);
      MockWebSocket.last().fail();
    });
    act(() => {
      vi.advanceTimersByTime(30000);
      MockWebSocket.last().open();
    });
    expect(logs).toEqual([]);
  });

  it("marks a drop with one gap so lines lost while down are labeled", () => {
    setup();
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        frame("log", { line: "before", stream: "stdout" }),
      );
    });
    act(() => {
      MockWebSocket.last().fail();
    });
    act(() => {
      vi.advanceTimersByTime(30000);
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        frame("log", { line: "after", stream: "stdout" }),
      );
    });
    expect(logs).toEqual([
      {
        id: expect.any(Number),
        kind: "line",
        line: "before",
        stripped: "before",
        stream: "stdout",
      },
      { id: expect.any(Number), kind: "gap" },
      {
        id: expect.any(Number),
        kind: "line",
        line: "after",
        stripped: "after",
        stream: "stdout",
      },
    ]);
  });

  it("does not stack gap markers across repeated failed reconnects", () => {
    setup();
    act(() => {
      MockWebSocket.last().open();
    });
    act(() => {
      MockWebSocket.last().fail();
    });
    for (let i = 0; i < 3; i++) {
      act(() => {
        vi.advanceTimersByTime(30000);
        MockWebSocket.last().fail();
      });
    }
    act(() => {
      vi.advanceTimersByTime(30000);
      MockWebSocket.last().open();
    });
    expect(logs).toEqual([{ id: expect.any(Number), kind: "gap" }]);
  });

  it("does not append a gap marker on a rotation reconnect", () => {
    setup();
    act(() => {
      MockWebSocket.last().open();
    });
    act(() => {
      setAccessToken("tok-2");
    });
    act(() => {
      MockWebSocket.last().open();
    });
    expect(logs).toEqual([]);
  });

  it("windows metrics samples to the last N", () => {
    setup();
    act(() => {
      for (let i = 0; i < METRICS_WINDOW + 5; i++) {
        MockWebSocket.last().message(
          frame("metrics", {
            cpu_millis: i,
            memory_bytes: i * 10,
            player_count: i,
          }),
        );
      }
    });
    expect(state.metrics).toHaveLength(METRICS_WINDOW);
    // The oldest five were dropped: the window starts at sample 5.
    expect(state.metrics[0].cpuMillis).toBe(5);
    expect(state.metrics.at(-1)?.cpuMillis).toBe(METRICS_WINDOW + 4);
  });

  it("clears windowed metrics when a status frame settles at rest", () => {
    setup();
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        frame("metrics", { cpu_millis: 1, memory_bytes: 2, player_count: 3 }),
      );
    });
    expect(state.metrics).toHaveLength(1);

    // The server stops mid-view: drop the stale samples so the strip can fall
    // back to the idle copy instead of showing frozen numbers forever.
    act(() => {
      MockWebSocket.last().message(
        frame("status", { state: "stopped", detail: "" }),
      );
    });
    expect(state.metrics).toEqual([]);
  });

  it("keeps windowed metrics while a status frame is still live", () => {
    setup();
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        frame("metrics", { cpu_millis: 1, memory_bytes: 2, player_count: 3 }),
      );
      MockWebSocket.last().message(
        frame("status", { state: "running", detail: "" }),
      );
    });
    expect(state.metrics).toHaveLength(1);
  });

  it("patches the detail query observed_state on a status frame", () => {
    const { queryClient } = setup();
    queryClient.setQueryData(serverKey(CID, SID), {
      id: SID,
      observed_state: "starting",
    });
    act(() => {
      MockWebSocket.last().message(
        frame("status", { state: "running", detail: "" }),
      );
    });
    expect(queryClient.getQueryData(serverKey(CID, SID))).toEqual({
      id: SID,
      observed_state: "running",
    });
  });

  it("patches the detail query observed_state from a snapshot frame", () => {
    const { queryClient } = setup();
    queryClient.setQueryData(serverKey(CID, SID), {
      id: SID,
      observed_state: "running",
    });
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(frame("snapshot", { state: "stopped" }));
    });
    expect(queryClient.getQueryData(serverKey(CID, SID))).toEqual({
      id: SID,
      observed_state: "stopped",
    });
  });

  it("clears windowed metrics when a snapshot shows the server at rest", () => {
    setup();
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        frame("metrics", { cpu_millis: 1, memory_bytes: 2, player_count: 3 }),
      );
    });
    expect(state.metrics).toHaveLength(1);
    act(() => {
      MockWebSocket.last().message(frame("snapshot", { state: "stopped" }));
    });
    expect(state.metrics).toEqual([]);
  });

  it("keeps the status detail across a snapshot of the same state", () => {
    // A reconnect's snapshot carries no detail (it is not persisted); the crash
    // reason the live transition carried still describes the state.
    setup();
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        frame("status", { state: "crashed", detail: "exit 1" }),
      );
      MockWebSocket.last().message(frame("snapshot", { state: "crashed" }));
    });
    expect(state.statusDetail).toBe("exit 1");
  });

  it("drops the status detail when a snapshot shows a different state", () => {
    setup();
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        frame("status", { state: "unknown", detail: "worker lost" }),
      );
      MockWebSocket.last().message(frame("snapshot", { state: "crashed" }));
    });
    expect(state.statusDetail).toBe("");
  });

  it("goes degraded on loss and refetches the detail query once (status only)", () => {
    const { invalidateSpy } = setup();
    act(() => {
      MockWebSocket.last().open();
    });
    expect(state.degraded).toBe(false);

    act(() => {
      MockWebSocket.last().fail();
    });
    expect(state.degraded).toBe(true);
    expect(invalidateSpy).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: serverKey(CID, SID),
    });

    // Reopen resyncs once: frames from the down window are never replayed.
    invalidateSpy.mockClear();
    act(() => {
      vi.advanceTimersByTime(30000);
      MockWebSocket.last().open();
    });
    expect(state.degraded).toBe(false);
    expect(invalidateSpy).toHaveBeenCalledTimes(1);

    // No log/metrics polling: advancing time issues no further refetches.
    invalidateSpy.mockClear();
    act(() => {
      vi.advanceTimersByTime(60000);
    });
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("does not refetch on the pristine initial open", () => {
    const { invalidateSpy } = setup();
    act(() => {
      MockWebSocket.last().open();
    });
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("refetches on the first open after failed initial connects", () => {
    const { invalidateSpy } = setup();
    act(() => {
      MockWebSocket.last().fail();
    });
    // The onDown fallback refetched at drop time; the open must resync again
    // because up to a full backoff window has passed since.
    invalidateSpy.mockClear();
    act(() => {
      vi.advanceTimersByTime(30000);
      MockWebSocket.last().open();
    });
    expect(invalidateSpy).toHaveBeenCalledTimes(1);
  });

  it("refetches the detail query on a rotation reconnect", () => {
    const { invalidateSpy } = setup();
    act(() => {
      MockWebSocket.last().open();
    });
    invalidateSpy.mockClear();
    act(() => {
      setAccessToken("tok-2");
    });
    act(() => {
      MockWebSocket.last().open();
    });
    expect(invalidateSpy).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: serverKey(CID, SID),
    });
  });

  it("refetches the detail query on a server-sent gap frame", () => {
    const { invalidateSpy } = setup();
    act(() => {
      MockWebSocket.last().open();
    });
    invalidateSpy.mockClear();
    act(() => {
      MockWebSocket.last().message(frame("gap", {}));
    });
    expect(invalidateSpy).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: serverKey(CID, SID),
    });
  });

  it("issues one refetch per transition across a flapping connection", () => {
    const { invalidateSpy } = setup();
    act(() => {
      MockWebSocket.last().open();
    });
    invalidateSpy.mockClear();
    for (let i = 0; i < 2; i++) {
      act(() => {
        MockWebSocket.last().fail(); // one refetch: onDown REST fallback
      });
      act(() => {
        vi.advanceTimersByTime(30000);
        MockWebSocket.last().open(); // one refetch: reopen resync
      });
    }
    expect(invalidateSpy).toHaveBeenCalledTimes(4);
  });

  it("reconnects with the fresh token on rotation", () => {
    setup();
    act(() => {
      MockWebSocket.last().open();
    });
    act(() => {
      setAccessToken("tok-2");
    });
    expect(MockWebSocket.last().protocols).toEqual(["access_token", "tok-2"]);
  });

  it("tears down the socket on unmount", () => {
    const { view } = setup();
    const socket = MockWebSocket.last();
    act(() => {
      socket.open();
    });
    act(() => {
      view.unmount();
    });
    expect(socket.closed).toBe(true);

    // A late failure does not reconnect.
    act(() => {
      socket.fail();
      vi.advanceTimersByTime(60000);
    });
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it("appends local RCON echoes into the buffer", () => {
    setup();
    act(() => {
      state.appendLocal([
        { kind: "command", line: "say hi" },
        { kind: "output", line: "done" },
      ]);
    });
    expect(logs).toEqual([
      {
        id: expect.any(Number),
        kind: "command",
        line: "say hi",
        stripped: "say hi",
      },
      {
        id: expect.any(Number),
        kind: "output",
        line: "done",
        stripped: "done",
      },
    ]);
  });

  it("exposes the latest status detail from status frames", () => {
    setup();
    expect(state.statusDetail).toBe("");

    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        frame("status", {
          state: "crashed",
          detail: "container exited unexpectedly",
        }),
      );
    });
    expect(state.statusDetail).toBe("container exited unexpectedly");

    // A new status frame replaces the previous detail.
    act(() => {
      MockWebSocket.last().message(
        frame("status", { state: "running", detail: "" }),
      );
    });
    expect(state.statusDetail).toBe("");
  });
});

type Detail = { id: string; observed_state: string };

/**
 * A detail query whose REST responses the test resolves by hand, so a response
 * can land after a WS frame that is newer than its read (#3213).
 */
function setupDeferred() {
  const pending: ((row: Detail) => void)[] = [];
  const queryFn = () =>
    new Promise<Detail>((resolve) => {
      pending.push(resolve);
    });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function LiveProbe() {
    // The page's order: the events hook, then the detail query.
    state = useServerEvents(CID, SID);
    useQuery({ queryKey: serverKey(CID, SID), queryFn });
    return null;
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  render(<LiveProbe />, { wrapper });
  // Resolve the oldest outstanding REST read with `observed_state`.
  const respond = async (observed_state: string) => {
    const resolve = pending.shift();
    expect(resolve).toBeDefined();
    await act(async () => {
      resolve?.({ id: SID, observed_state });
      await vi.advanceTimersByTimeAsync(0);
    });
  };
  const cached = () =>
    queryClient.getQueryData<Detail>(serverKey(CID, SID))?.observed_state;
  return { queryClient, respond, cached, pending };
}

describe("useServerEvents with REST reads in flight (#3213)", () => {
  let restore: () => void;

  beforeEach(() => {
    vi.useFakeTimers();
    restore = installMockWebSocket();
    setAccessToken("tok-1");
  });

  afterEach(() => {
    restore();
    clearAccessToken();
    vi.useRealTimers();
  });

  it("keeps a snapshot that arrives before the cold detail load lands", async () => {
    const { respond, cached } = setupDeferred();
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(frame("snapshot", { state: "running" }));
    });
    // The cold read captured the state before the transition.
    await respond("stopped");
    expect(cached()).toBe("running");
  });

  it("keeps a status frame that arrives before the cold detail load lands", async () => {
    const { respond, cached } = setupDeferred();
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        frame("status", { state: "starting", detail: "" }),
      );
    });
    await respond("stopped");
    expect(cached()).toBe("starting");
  });

  it("keeps a reconnect snapshot over the older drop/reopen refetch response", async () => {
    const { respond, cached, pending } = setupDeferred();
    act(() => {
      MockWebSocket.last().open();
    });
    await respond("running");
    // The drop starts a status-only refetch whose read is still in flight
    // when the reconnect's snapshot lands.
    act(() => {
      MockWebSocket.last().fail();
    });
    act(() => {
      vi.advanceTimersByTime(30000);
      MockWebSocket.last().open();
      MockWebSocket.last().message(frame("snapshot", { state: "stopped" }));
    });
    while (pending.length > 0) {
      await respond("running");
    }
    expect(cached()).toBe("stopped");
  });

  it("lets a REST read started after the frame supersede it", async () => {
    // A worker disconnect commits `unknown` without a frame; a read that
    // started after the last frame is the newer truth.
    const { queryClient, respond, cached } = setupDeferred();
    act(() => {
      MockWebSocket.last().open();
    });
    await respond("stopped");
    act(() => {
      MockWebSocket.last().message(
        frame("status", { state: "running", detail: "" }),
      );
    });
    act(() => {
      queryClient.invalidateQueries({ queryKey: serverKey(CID, SID) });
    });
    await respond("unknown");
    expect(cached()).toBe("unknown");
  });
});
