import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  resetForTesting as resetClientForTesting,
  setRefresher,
} from "../api/client.ts";
import {
  hardLogout,
  refreshForRetry,
  resetForTesting as resetSessionForTesting,
  signIn,
} from "../auth/session.ts";
import {
  clearAccessToken,
  getAccessToken,
  getAuthEpoch,
  setAccessToken,
} from "../auth/tokenStore.ts";
import { installMockWebSocket, MockWebSocket } from "../test/mockWebSocket.ts";
import {
  backoffDelayMs,
  CommunityEventsClient,
  parseCommunityFrame,
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

function gapFrame() {
  return JSON.stringify({
    stream: "gap",
    ts: "t",
    payload: {},
    server_id: null,
  });
}

function snapshotFrame(servers: { server_id: string; state: string }[]) {
  return JSON.stringify({
    stream: "snapshot",
    ts: "2026-06-06T00:00:00Z",
    payload: { servers },
    server_id: null,
  });
}

function notificationFrame(
  serverId: string | null,
  kind: string,
  title: string,
  detail: string,
) {
  return JSON.stringify({
    stream: "notification",
    ts: "2026-06-06T00:00:00Z",
    payload: { kind, title, detail },
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

describe("parseCommunityFrame", () => {
  it("parses a status frame to {kind, serverId, state}", () => {
    expect(parseCommunityFrame(statusFrame("s1", "running"))).toEqual({
      kind: "status",
      serverId: "s1",
      state: "running",
    });
  });

  it("parses a snapshot frame to {kind, servers: [{serverId, state}]}", () => {
    expect(
      parseCommunityFrame(
        snapshotFrame([
          { server_id: "s1", state: "running" },
          { server_id: "s2", state: "stopped" },
        ]),
      ),
    ).toEqual({
      kind: "snapshot",
      servers: [
        { serverId: "s1", state: "running" },
        { serverId: "s2", state: "stopped" },
      ],
    });
  });

  it("parses an empty snapshot (a community with no servers)", () => {
    expect(parseCommunityFrame(snapshotFrame([]))).toEqual({
      kind: "snapshot",
      servers: [],
    });
  });

  it("drops a malformed snapshot frame", () => {
    const snapshot = (payload: unknown) =>
      JSON.stringify({ stream: "snapshot", ts: "t", payload, server_id: null });
    expect(parseCommunityFrame(snapshot({}))).toBeNull();
    expect(parseCommunityFrame(snapshot({ servers: "s1" }))).toBeNull();
    expect(
      parseCommunityFrame(snapshot({ servers: [{ server_id: "s1" }] })),
    ).toBeNull();
    expect(
      parseCommunityFrame(snapshot({ servers: [{ state: "running" }] })),
    ).toBeNull();
  });

  it("parses the server-agnostic GAP marker", () => {
    expect(parseCommunityFrame(gapFrame())).toEqual({ kind: "gap" });
  });

  it("parses a notification frame to {kind, serverId, notificationKind, title, detail}", () => {
    expect(
      parseCommunityFrame(
        notificationFrame(
          "s1",
          "schedule_failed",
          "Scheduled stop failed",
          "worker_unavailable",
        ),
      ),
    ).toEqual({
      kind: "notification",
      serverId: "s1",
      notificationKind: "schedule_failed",
      title: "Scheduled stop failed",
      detail: "worker_unavailable",
    });
  });

  it("drops a notification frame missing a payload title", () => {
    expect(
      parseCommunityFrame(
        JSON.stringify({
          stream: "notification",
          ts: "t",
          payload: { kind: "schedule_failed" },
          server_id: "s1",
        }),
      ),
    ).toBeNull();
  });

  it("drops a non-status/non-notification stream and malformed input", () => {
    expect(
      parseCommunityFrame(JSON.stringify({ stream: "log", server_id: "s1" })),
    ).toBeNull();
    expect(parseCommunityFrame("not json")).toBeNull();
    expect(
      parseCommunityFrame(
        JSON.stringify({ stream: "status", server_id: "s1" }),
      ),
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
    resetClientForTesting();
    resetSessionForTesting();
    vi.unstubAllGlobals();
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
    const onGap = vi.fn();
    const onNotification = vi.fn();
    const onSnapshot = vi.fn();
    const onOpen = vi.fn();
    const onDown = vi.fn();
    return {
      callbacks: {
        onStatus,
        onGap,
        onNotification,
        onSnapshot,
        onOpen,
        onDown,
      },
      onStatus,
      onSnapshot,
      onGap,
      onNotification,
      onOpen,
      onDown,
    };
  }

  it("connects with the access token in the subprotocol header", () => {
    const { client } = makeClient();
    client.start();
    expect(MockWebSocket.last().url).toContain(
      `/api/communities/${CID}/events`,
    );
    expect(MockWebSocket.last().url).not.toContain("token=");
    expect(MockWebSocket.last().protocols).toEqual(["access_token", "tok-1"]);
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

  it("fires onGap on a gap frame, not onStatus", () => {
    const { client, onGap, onStatus } = makeClient();
    client.start();
    MockWebSocket.last().open();

    MockWebSocket.last().message(gapFrame());
    expect(onGap).toHaveBeenCalledTimes(1);
    expect(onStatus).not.toHaveBeenCalled();
    client.close();
  });

  it("fires onSnapshot with every server's state on a snapshot frame", () => {
    const { client, onSnapshot, onStatus } = makeClient();
    client.start();
    MockWebSocket.last().open();

    MockWebSocket.last().message(
      snapshotFrame([{ server_id: "s1", state: "crashed" }]),
    );
    expect(onSnapshot).toHaveBeenCalledWith([
      { serverId: "s1", state: "crashed" },
    ]);
    expect(onStatus).not.toHaveBeenCalled();
    client.close();
  });

  it("fires onNotification on a notification frame, not onStatus", () => {
    const { client, onNotification, onStatus } = makeClient();
    client.start();
    MockWebSocket.last().open();

    MockWebSocket.last().message(
      JSON.stringify({
        stream: "notification",
        ts: "t",
        payload: {
          kind: "schedule_failed",
          title: "Scheduled restart failed",
          detail: "worker_unavailable",
        },
        server_id: "s1",
      }),
    );
    expect(onNotification).toHaveBeenCalledWith({
      serverId: "s1",
      notificationKind: "schedule_failed",
      title: "Scheduled restart failed",
      detail: "worker_unavailable",
    });
    expect(onStatus).not.toHaveBeenCalled();
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
    expect(first.protocols).toEqual(["access_token", "tok-1"]);

    setAccessToken("tok-2"); // rotation
    expect(first.closed).toBe(true);
    const second = MockWebSocket.last();
    expect(MockWebSocket.instances).toHaveLength(2);
    expect(second.protocols).toEqual(["access_token", "tok-2"]);
    client.close();
  });

  // The API closes the socket with 4419 when the token it was opened with
  // expires (#1862): reconnecting with that token would only fail the
  // handshake, so the session is refreshed first.
  it("refreshes the session on a 4419 close and reconnects with the fresh token", async () => {
    const refresher = vi.fn(async () => {
      setAccessToken("tok-2");
      return true;
    });
    setRefresher(refresher);
    const { client, onDown } = makeClient();
    client.start();
    MockWebSocket.last().open();

    MockWebSocket.last().serverClose(4419);
    expect(onDown).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(0);

    expect(refresher).toHaveBeenCalledTimes(1);
    expect(MockWebSocket.instances).toHaveLength(2);
    expect(MockWebSocket.last().protocols).toEqual(["access_token", "tok-2"]);
    client.close();
  });

  it("never reconnects with the expired token when the refresh fails", async () => {
    const refresher = vi.fn(async () => false); // transient: token unchanged
    setRefresher(refresher);
    const { client } = makeClient();
    client.start();
    MockWebSocket.last().open();

    MockWebSocket.last().serverClose(4419);
    await vi.advanceTimersByTimeAsync(60000);

    // The refresh is retried on the backoff; the expired token is never offered.
    expect(refresher.mock.calls.length).toBeGreaterThan(1);
    expect(MockWebSocket.instances).toHaveLength(1);

    refresher.mockImplementation(async () => {
      setAccessToken("tok-2");
      return true;
    });
    await vi.advanceTimersByTimeAsync(60000);
    expect(MockWebSocket.instances).toHaveLength(2);
    expect(MockWebSocket.last().protocols).toEqual(["access_token", "tok-2"]);
    client.close();
  });

  // The socket refreshes through the same session core as the REST client, so
  // a refresh it began before the session changed must not hand it the old
  // user's token to reconnect with (#3224).
  it("never reconnects as the previous user when its refresh lands after a new sign-in", async () => {
    const pendingFetches: ((response: Response) => void)[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            pendingFetches.push(resolve);
          }),
      ),
    );
    setRefresher(refreshForRetry);
    const { client } = makeClient();
    client.start();
    MockWebSocket.last().open();
    MockWebSocket.last().serverClose(4419);
    await vi.advanceTimersByTimeAsync(0);
    expect(pendingFetches).toHaveLength(1);

    hardLogout();
    signIn("tok-B");
    pendingFetches[0](
      new Response(
        JSON.stringify({
          access_token: "late-tok-1",
          refresh_token: "ignored",
          token_type: "bearer",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    await vi.advanceTimersByTimeAsync(60000);

    expect(getAccessToken()).toBe("tok-B");
    const offered = MockWebSocket.instances.map((s) => s.protocols);
    expect(offered).not.toContainEqual(["access_token", "late-tok-1"]);
    client.close();
  });

  it("hands the refresher the epoch of the session whose token expired", async () => {
    const refresher = vi.fn(async () => false);
    setRefresher(refresher);
    signIn("tok-A");
    const { client } = makeClient();
    client.start();
    MockWebSocket.last().open();

    MockWebSocket.last().serverClose(4419);
    await vi.advanceTimersByTimeAsync(0);

    expect(refresher).toHaveBeenCalledWith(getAuthEpoch());
    client.close();
  });

  it("does not refresh on an ordinary close", async () => {
    const refresher = vi.fn(async () => true);
    setRefresher(refresher);
    const { client } = makeClient();
    client.start();
    MockWebSocket.last().open();

    MockWebSocket.last().fail();
    await vi.advanceTimersByTimeAsync(500);

    expect(refresher).not.toHaveBeenCalled();
    expect(MockWebSocket.instances).toHaveLength(2);
    expect(MockWebSocket.last().protocols).toEqual(["access_token", "tok-1"]);
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
