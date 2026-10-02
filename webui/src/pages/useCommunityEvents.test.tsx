import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken, setAccessToken } from "../auth/tokenStore.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { installMockWebSocket, MockWebSocket } from "../test/mockWebSocket.ts";
import { serversKey, useCommunityEvents } from "./useCommunityEvents.ts";

const CID = "c1";

function statusFrame(serverId: string, state: string) {
  return JSON.stringify({
    stream: "status",
    ts: "t",
    payload: { state, detail: "" },
    server_id: serverId,
  });
}

function gapFrame() {
  return JSON.stringify({
    stream: "gap",
    ts: "t",
    payload: {},
    server_id: null,
  });
}

function snapshotFrame(servers: [string, string][]) {
  return JSON.stringify({
    stream: "snapshot",
    ts: "t",
    payload: {
      servers: servers.map(([server_id, state]) => ({ server_id, state })),
    },
    server_id: null,
  });
}

function notificationFrame(serverId: string, title: string, detail: string) {
  return JSON.stringify({
    stream: "notification",
    ts: "t",
    payload: { kind: "schedule_failed", title, detail },
    server_id: serverId,
  });
}

function serverRow(id: string, observed_state: string) {
  return { id, observed_state };
}

let degradedSeen = false;

function Probe({ communityId }: { communityId: string }) {
  degradedSeen = useCommunityEvents(communityId);
  return null;
}

function setup(seed: ReturnType<typeof serverRow>[] | undefined) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  if (seed !== undefined) {
    queryClient.setQueryData(serversKey(CID), seed);
  }
  const refetchSpy = vi.fn();
  // Stand in for the list query's refetch: invalidate calls into the client.
  queryClient.getQueryCache().subscribe((event) => {
    if (event.type === "updated" && event.action.type === "invalidate") {
      refetchSpy();
    }
  });
  // Counts resync requests by call. (The cache-event spy undercounts repeats:
  // with no observer to refetch and clear `isInvalidated`, only the first
  // invalidation of a query emits an "invalidate" event.)
  const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>{children}</ToastProvider>
    </QueryClientProvider>
  );
  render(<Probe communityId={CID} />, { wrapper });
  return { queryClient, refetchSpy, invalidateSpy };
}

describe("useCommunityEvents", () => {
  let restore: () => void;

  beforeEach(() => {
    vi.useFakeTimers();
    restore = installMockWebSocket();
    setAccessToken("tok-1");
    degradedSeen = false;
  });

  afterEach(() => {
    restore();
    clearAccessToken();
    vi.useRealTimers();
  });

  it("patches the cached server's observed_state on a status event", () => {
    const { queryClient } = setup([serverRow("s1", "stopped")]);
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(statusFrame("s1", "running"));
    });
    expect(queryClient.getQueryData(serversKey(CID))).toEqual([
      { id: "s1", observed_state: "running" },
    ]);
  });

  it("patches every cached server from a snapshot frame without a refetch", () => {
    const { queryClient, invalidateSpy } = setup([
      serverRow("s1", "stopped"),
      serverRow("s2", "running"),
    ]);
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        snapshotFrame([
          ["s1", "running"],
          ["s2", "crashed"],
        ]),
      );
    });
    expect(queryClient.getQueryData(serversKey(CID))).toEqual([
      { id: "s1", observed_state: "running" },
      { id: "s2", observed_state: "crashed" },
    ]);
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("converges from the snapshot on a reconnect", () => {
    const { queryClient } = setup([serverRow("s1", "running")]);
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().fail();
    });
    act(() => {
      vi.advanceTimersByTime(30000);
    });
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(snapshotFrame([["s1", "stopped"]]));
    });
    expect(queryClient.getQueryData(serversKey(CID))).toEqual([
      { id: "s1", observed_state: "stopped" },
    ]);
  });

  it("refetches the list when the snapshot names a server not loaded", () => {
    const { queryClient, invalidateSpy } = setup([serverRow("s1", "stopped")]);
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        snapshotFrame([
          ["s1", "running"],
          ["s2", "starting"],
        ]),
      );
    });
    expect(queryClient.getQueryData(serversKey(CID))).toEqual([
      { id: "s1", observed_state: "running" },
    ]);
    expect(invalidateSpy).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: serversKey(CID) });
  });

  it("refetches the list when a loaded server is missing from the snapshot", () => {
    const { invalidateSpy } = setup([
      serverRow("s1", "stopped"),
      serverRow("s2", "stopped"),
    ]);
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(snapshotFrame([["s1", "stopped"]]));
    });
    expect(invalidateSpy).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: serversKey(CID) });
  });

  it("ignores a snapshot that arrives before the list is loaded", () => {
    const { queryClient, invalidateSpy } = setup(undefined);
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(snapshotFrame([["s1", "running"]]));
    });
    expect(queryClient.getQueryData(serversKey(CID))).toBeUndefined();
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("refetches the list for an unknown server (created after load)", () => {
    const { refetchSpy } = setup([serverRow("s1", "running")]);
    refetchSpy.mockClear();
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(statusFrame("s2", "starting"));
    });
    expect(refetchSpy).toHaveBeenCalledTimes(1);
  });

  it("engages degraded polling after a WS failure", () => {
    const { refetchSpy } = setup([serverRow("s1", "running")]);
    act(() => {
      MockWebSocket.last().open();
    });
    expect(degradedSeen).toBe(false);

    act(() => {
      MockWebSocket.last().fail();
    });
    expect(degradedSeen).toBe(true);

    refetchSpy.mockClear();
    act(() => {
      vi.advanceTimersByTime(10000);
    });
    expect(refetchSpy).toHaveBeenCalledTimes(1);
  });

  it("disengages degraded mode and stops polling on WS recovery", () => {
    const { refetchSpy } = setup([serverRow("s1", "running")]);
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().fail();
    });
    expect(degradedSeen).toBe(true);

    // The reconnect timer fires; the new socket opens -> recovery.
    act(() => {
      vi.advanceTimersByTime(30000);
    });
    act(() => {
      MockWebSocket.last().open();
    });
    expect(degradedSeen).toBe(false);

    refetchSpy.mockClear();
    act(() => {
      vi.advanceTimersByTime(20000);
    });
    expect(refetchSpy).not.toHaveBeenCalled();
  });

  it("does not refetch on the pristine initial open", () => {
    const { invalidateSpy } = setup([serverRow("s1", "running")]);
    act(() => {
      MockWebSocket.last().open();
    });
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("refetches the list once on WS recovery", () => {
    const { invalidateSpy } = setup([serverRow("s1", "running")]);
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().fail();
    });
    act(() => {
      vi.advanceTimersByTime(30000); // poll ticks fire; reconnect timer fires
    });
    // Transitions between the last poll tick and the reopen would otherwise
    // be lost for good: the reopen itself must reconcile once.
    invalidateSpy.mockClear();
    act(() => {
      MockWebSocket.last().open();
    });
    expect(invalidateSpy).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: serversKey(CID) });
  });

  it("refetches the list once on a rotation reconnect", () => {
    const { invalidateSpy } = setup([serverRow("s1", "running")]);
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
  });

  it("surfaces a notification frame as a failure toast", () => {
    setup([serverRow("s1", "running")]);
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        notificationFrame("s1", "Scheduled stop failed", "worker_unavailable"),
      );
    });
    const toast = screen.getByRole("status");
    expect(toast).toHaveTextContent("Scheduled stop failed");
    expect(toast).toHaveTextContent("worker_unavailable");
    expect(toast.className).toContain("error");
  });

  it("does not patch the servers cache for a notification frame", () => {
    const { queryClient } = setup([serverRow("s1", "running")]);
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        notificationFrame("s1", "Scheduled stop failed", ""),
      );
    });
    // A notification is not a status change: the cached row is untouched.
    expect(queryClient.getQueryData(serversKey(CID))).toEqual([
      { id: "s1", observed_state: "running" },
    ]);
  });

  it("refetches the list on a gap frame (dropped status frames)", () => {
    const { invalidateSpy } = setup([serverRow("s1", "running")]);
    act(() => {
      MockWebSocket.last().open();
    });
    invalidateSpy.mockClear();
    act(() => {
      MockWebSocket.last().message(gapFrame());
    });
    expect(invalidateSpy).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: serversKey(CID) });
  });
});

type Row = ReturnType<typeof serverRow>;

/**
 * A servers-list query whose REST responses the test resolves by hand, so a
 * response can land after a WS frame that is newer than its read (#3213).
 */
function setupDeferred() {
  const pending: ((rows: Row[]) => void)[] = [];
  const queryFn = () =>
    new Promise<Row[]>((resolve) => {
      pending.push(resolve);
    });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function LiveProbe() {
    // The page's order: the events hook, then the list query.
    degradedSeen = useCommunityEvents(CID);
    useQuery({ queryKey: serversKey(CID), queryFn });
    return null;
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>{children}</ToastProvider>
    </QueryClientProvider>
  );
  render(<LiveProbe />, { wrapper });
  // Resolve the oldest outstanding REST read with `rows`.
  const respond = async (rows: Row[]) => {
    const resolve = pending.shift();
    expect(resolve).toBeDefined();
    await act(async () => {
      resolve?.(rows);
      await vi.advanceTimersByTimeAsync(0);
    });
  };
  const cached = () => queryClient.getQueryData<Row[]>(serversKey(CID));
  return { queryClient, respond, cached, pending };
}

describe("useCommunityEvents with REST reads in flight (#3213)", () => {
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

  it("keeps a snapshot that arrives before the cold list load lands", async () => {
    const { respond, cached } = setupDeferred();
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(snapshotFrame([["s1", "running"]]));
    });
    // The cold read captured the state before the transition.
    await respond([serverRow("s1", "stopped")]);
    expect(cached()).toEqual([{ id: "s1", observed_state: "running" }]);
  });

  it("keeps a status frame that arrives before the cold list load lands", async () => {
    const { respond, cached } = setupDeferred();
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(statusFrame("s1", "starting"));
    });
    await respond([serverRow("s1", "stopped")]);
    expect(cached()).toEqual([{ id: "s1", observed_state: "starting" }]);
  });

  it("keeps a reconnect snapshot over the older reopen refetch response", async () => {
    const { respond, cached, pending } = setupDeferred();
    act(() => {
      MockWebSocket.last().open();
    });
    await respond([serverRow("s1", "running")]);
    act(() => {
      MockWebSocket.last().fail();
    });
    // The drop's poll ticks and the reconnect timer fire; settle each read.
    act(() => {
      vi.advanceTimersByTime(30000);
    });
    while (pending.length > 0) {
      await respond([serverRow("s1", "running")]);
    }
    // The reopen starts a refetch; the snapshot lands before its response.
    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(snapshotFrame([["s1", "stopped"]]));
    });
    await respond([serverRow("s1", "running")]);
    expect(cached()).toEqual([{ id: "s1", observed_state: "stopped" }]);
  });

  it("lets a REST read started after the frame supersede it", async () => {
    // A worker disconnect commits `unknown` without a frame; a read that
    // started after the last frame is the newer truth.
    const { queryClient, respond, cached } = setupDeferred();
    act(() => {
      MockWebSocket.last().open();
    });
    await respond([serverRow("s1", "stopped")]);
    act(() => {
      MockWebSocket.last().message(statusFrame("s1", "running"));
    });
    act(() => {
      queryClient.invalidateQueries({ queryKey: serversKey(CID) });
    });
    await respond([serverRow("s1", "unknown")]);
    expect(cached()).toEqual([{ id: "s1", observed_state: "unknown" }]);
  });
});
