import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken, setAccessToken } from "../auth/tokenStore.ts";
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
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  render(<Probe communityId={CID} />, { wrapper });
  return { queryClient, refetchSpy };
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
});
