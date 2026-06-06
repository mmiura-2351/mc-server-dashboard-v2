import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken, setAccessToken } from "../auth/tokenStore.ts";
import { installMockWebSocket, MockWebSocket } from "../test/mockWebSocket.ts";
import { serverKey } from "./serverKey.ts";
import {
  METRICS_WINDOW,
  type ServerEventsState,
  useServerEvents,
} from "./useServerEvents.ts";

const CID = "c1";
const SID = "s1";

function frame(stream: string, payload: unknown) {
  return JSON.stringify({ stream, ts: "t", payload });
}

let state: ServerEventsState;

function Probe() {
  state = useServerEvents(CID, SID);
  return null;
}

function setup() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const refetchSpy = vi.fn();
  queryClient.getQueryCache().subscribe((event) => {
    if (event.type === "updated" && event.action.type === "invalidate") {
      refetchSpy();
    }
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const view = render(<Probe />, { wrapper });
  return { queryClient, refetchSpy, view };
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
    expect(state.logs).toEqual([
      { id: expect.any(Number), kind: "line", line: "a", stream: "stdout" },
      { id: expect.any(Number), kind: "line", line: "b", stream: "stderr" },
    ]);
  });

  it("appends a gap marker into the log buffer", () => {
    setup();
    act(() => {
      MockWebSocket.last().message(frame("gap", {}));
    });
    expect(state.logs).toEqual([{ id: expect.any(Number), kind: "gap" }]);
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

  it("goes degraded on loss and refetches the detail query once (status only)", () => {
    const { queryClient, refetchSpy } = setup();
    queryClient.setQueryData(serverKey(CID, SID), {
      id: SID,
      observed_state: "running",
    });
    act(() => {
      MockWebSocket.last().open();
    });
    expect(state.degraded).toBe(false);

    refetchSpy.mockClear();
    act(() => {
      MockWebSocket.last().fail();
    });
    expect(state.degraded).toBe(true);
    expect(refetchSpy).toHaveBeenCalledTimes(1);

    // No log/metrics polling: advancing time issues no further refetches.
    refetchSpy.mockClear();
    act(() => {
      vi.advanceTimersByTime(30000);
      MockWebSocket.last().open();
    });
    expect(state.degraded).toBe(false);
    expect(refetchSpy).not.toHaveBeenCalled();
  });

  it("reconnects with the fresh token on rotation", () => {
    setup();
    act(() => {
      MockWebSocket.last().open();
    });
    act(() => {
      setAccessToken("tok-2");
    });
    expect(MockWebSocket.last().url).toContain("token=tok-2");
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
    expect(state.logs).toEqual([
      { id: expect.any(Number), kind: "command", line: "say hi" },
      { id: expect.any(Number), kind: "output", line: "done" },
    ]);
  });
});
