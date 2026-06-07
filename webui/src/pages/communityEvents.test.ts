import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken, setAccessToken } from "../auth/tokenStore.ts";
import { installMockWebSocket, MockWebSocket } from "../test/mockWebSocket.ts";
import {
  backoffDelayMs,
  CommunityEventsClient,
  parseStatusFrame,
} from "./communityEvents.ts";

const CID = "c1";

function statusFrame(serverId: string, state: string) {
  return JSON.stringify({
    stream: "status",
    ts: "2026-06-06T00:00:00Z",
    payload: { state, detail: "" },
    server_id: serverId,
  });
}

describe("backoffDelayMs", () => {
  it("doubles the cap each attempt, capped at 30s", () => {
    // random()=1 would be the open upper bound; use just-below-1 to read the
    // capped step (full jitter floors random*step).
    const r = () => 0.999999;
    expect(backoffDelayMs(1, r)).toBe(999); // step 1000
    expect(backoffDelayMs(2, r)).toBe(1999); // step 2000
    expect(backoffDelayMs(3, r)).toBe(3999); // step 4000
    expect(backoffDelayMs(6, r)).toBe(31999 - 2000); // step capped: 30000-ish
    expect(backoffDelayMs(10, r)).toBeLessThanOrEqual(30000);
    expect(backoffDelayMs(10, r)).toBeGreaterThanOrEqual(29999);
  });

  it("keeps full jitter within [0, step]", () => {
    expect(backoffDelayMs(1, () => 0)).toBe(0);
    expect(backoffDelayMs(5, () => 0)).toBe(0);
    // Mid jitter is bounded by the step.
    expect(backoffDelayMs(3, () => 0.5)).toBe(2000); // 0.5 * 4000
  });
});

describe("parseStatusFrame", () => {
  it("parses a status frame to {serverId, state}", () => {
    expect(parseStatusFrame(statusFrame("s1", "running"))).toEqual({
      serverId: "s1",
      state: "running",
    });
  });

  it("drops a GAP frame (no server_id)", () => {
    expect(
      parseStatusFrame(
        JSON.stringify({
          stream: "gap",
          ts: "t",
          payload: {},
          server_id: null,
        }),
      ),
    ).toBeNull();
  });

  it("drops a non-status stream and malformed input", () => {
    expect(
      parseStatusFrame(JSON.stringify({ stream: "log", server_id: "s1" })),
    ).toBeNull();
    expect(parseStatusFrame("not json")).toBeNull();
    expect(
      parseStatusFrame(JSON.stringify({ stream: "status", server_id: "s1" })),
    ).toBeNull();
  });
});

describe("CommunityEventsClient", () => {
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

  function makeClient(
    overrides: Partial<Parameters<typeof callbacks>[0]> = {},
  ) {
    const cb = callbacks(overrides);
    const client = new CommunityEventsClient(CID, cb.callbacks, () => 0.5);
    return { client, ...cb };
  }

  function callbacks(
    overrides: {
      onStatus?: (e: { serverId: string; state: string }) => void;
    } = {},
  ) {
    const onStatus = vi.fn(overrides.onStatus);
    const onOpen = vi.fn();
    const onDown = vi.fn();
    return {
      callbacks: { onStatus, onOpen, onDown },
      onStatus,
      onOpen,
      onDown,
    };
  }

  it("connects with the access token in the ?token= query", () => {
    const { client } = makeClient();
    client.start();
    expect(MockWebSocket.last().url).toContain(
      `/api/communities/${CID}/events`,
    );
    expect(MockWebSocket.last().url).toContain("token=tok-1");
    client.close();
  });

  it("fires onOpen on connect and onStatus for a parsed frame", () => {
    const { client, onOpen, onStatus } = makeClient();
    client.start();
    MockWebSocket.last().open();
    expect(onOpen).toHaveBeenCalledTimes(1);

    MockWebSocket.last().message(statusFrame("s1", "running"));
    expect(onStatus).toHaveBeenCalledWith({ serverId: "s1", state: "running" });
    client.close();
  });

  it("reconnects on close with backoff and resets backoff on open", () => {
    const { client, onDown, onOpen } = makeClient();
    client.start();
    const first = MockWebSocket.last();
    first.open(); // attempt reset to 0
    expect(onOpen).toHaveBeenCalledTimes(1);

    first.fail();
    expect(onDown).toHaveBeenCalledTimes(1);
    // attempt 1: step 1000, random 0.5 -> 500ms.
    vi.advanceTimersByTime(499);
    expect(MockWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(MockWebSocket.instances).toHaveLength(2);

    // A second failure before any open escalates the backoff to attempt 2.
    MockWebSocket.last().fail();
    vi.advanceTimersByTime(999); // step 2000 * 0.5 = 1000
    expect(MockWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(MockWebSocket.instances).toHaveLength(3);

    // Opening resets the backoff: the next failure is attempt 1 again (500ms).
    MockWebSocket.last().open();
    MockWebSocket.last().fail();
    vi.advanceTimersByTime(500);
    expect(MockWebSocket.instances).toHaveLength(4);
    client.close();
  });

  it("tears down cleanly on close: no socket, no reconnect", () => {
    const { client, onDown } = makeClient();
    client.start();
    const socket = MockWebSocket.last();
    socket.open();

    client.close();
    expect(socket.closed).toBe(true);

    // A late close event from the torn-down socket does not reconnect.
    socket.fail();
    vi.advanceTimersByTime(60000);
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(onDown).not.toHaveBeenCalled();
  });

  it("reconnects with the fresh token on rotation", () => {
    const { client } = makeClient();
    client.start();
    const first = MockWebSocket.last();
    first.open();
    expect(first.url).toContain("token=tok-1");

    setAccessToken("tok-2"); // rotation
    expect(first.closed).toBe(true);
    const second = MockWebSocket.last();
    expect(MockWebSocket.instances).toHaveLength(2);
    expect(second.url).toContain("token=tok-2");
    client.close();
  });

  it("ignores a rotation to the same token", () => {
    const { client } = makeClient();
    client.start();
    MockWebSocket.last().open();

    setAccessToken("tok-1"); // no-op: same value, no rotation fired
    expect(MockWebSocket.instances).toHaveLength(1);
    client.close();
  });
});
