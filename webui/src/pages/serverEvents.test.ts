import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken, setAccessToken } from "../auth/tokenStore.ts";
import { installMockWebSocket, MockWebSocket } from "../test/mockWebSocket.ts";
import {
  parseServerFrame,
  ServerEventsClient,
  serverEventsUrl,
} from "./serverEvents.ts";

const CID = "c1";
const SID = "s1";

function frame(stream: string, payload: unknown) {
  return JSON.stringify({ stream, ts: "2026-06-06T00:00:00Z", payload });
}

describe("serverEventsUrl", () => {
  it("builds the events path with the streams comma list (no token)", () => {
    const url = serverEventsUrl(CID, SID, ["status", "log", "metrics"]);
    expect(url).toContain(`/api/communities/${CID}/servers/${SID}/events`);
    expect(url).toContain("streams=status%2Clog%2Cmetrics");
    expect(url).not.toContain("token=");
  });
});

describe("parseServerFrame", () => {
  it("parses a status frame with state + detail", () => {
    expect(
      parseServerFrame(frame("status", { state: "running", detail: "ok" })),
    ).toEqual({ kind: "status", state: "running", detail: "ok", reason: "" });
  });

  it("defaults a missing status detail to empty", () => {
    expect(parseServerFrame(frame("status", { state: "running" }))).toEqual({
      kind: "status",
      state: "running",
      detail: "",
      reason: "",
    });
  });

  it("parses the crash reason a status frame carries", () => {
    expect(
      parseServerFrame(
        frame("status", {
          state: "crashed",
          detail: "boom",
          reason: "forge_install_out_of_memory",
        }),
      ),
    ).toEqual({
      kind: "status",
      state: "crashed",
      detail: "boom",
      reason: "forge_install_out_of_memory",
    });
  });

  it("reads a non-string crash reason as unclassified", () => {
    expect(
      parseServerFrame(frame("status", { state: "crashed", reason: 2 })),
    ).toMatchObject({ reason: "" });
  });

  it("parses a log frame and its std stream", () => {
    expect(
      parseServerFrame(frame("log", { line: "hello", stream: "stderr" })),
    ).toEqual({ kind: "log", line: "hello", stream: "stderr" });
    // An unknown/absent std stream falls back to stdout.
    expect(parseServerFrame(frame("log", { line: "hi" }))).toEqual({
      kind: "log",
      line: "hi",
      stream: "stdout",
    });
  });

  it("parses a metrics frame", () => {
    expect(
      parseServerFrame(
        frame("metrics", {
          cpu_millis: 250,
          memory_bytes: 1024,
          player_count: 3,
        }),
      ),
    ).toEqual({
      kind: "metrics",
      cpuMillis: 250,
      memoryBytes: 1024,
      playerCount: 3,
    });
  });

  it("parses a snapshot frame to the subscribe-time observed state", () => {
    expect(parseServerFrame(frame("snapshot", { state: "crashed" }))).toEqual({
      kind: "snapshot",
      state: "crashed",
    });
  });

  it("parses the intent a status frame carries (#3428)", () => {
    expect(
      parseServerFrame(frame("status", { desired_state: "running" })),
    ).toEqual({
      kind: "status",
      desiredState: "running",
      detail: "",
      reason: "",
    });
  });

  it("parses the desired state a snapshot frame carries (#3428)", () => {
    expect(
      parseServerFrame(
        frame("snapshot", { state: "crashed", desired_state: "running" }),
      ),
    ).toEqual({ kind: "snapshot", state: "crashed", desiredState: "running" });
  });

  it("parses the assignment a status frame carries (#3818)", () => {
    expect(
      parseServerFrame(frame("status", { assigned_worker_id: null })),
    ).toEqual({
      kind: "status",
      assignedWorkerId: null,
      detail: "",
      reason: "",
    });
    expect(
      parseServerFrame(
        frame("status", { desired_state: "running", assigned_worker_id: "w1" }),
      ),
    ).toEqual({
      kind: "status",
      desiredState: "running",
      assignedWorkerId: "w1",
      detail: "",
      reason: "",
    });
  });

  it("leaves the assignment out of a status frame that does not carry it", () => {
    expect(
      parseServerFrame(frame("status", { state: "running" })),
    ).not.toHaveProperty("assignedWorkerId");
    expect(
      parseServerFrame(frame("status", { assigned_worker_id: 7 })),
    ).toBeNull();
  });

  it("parses the assignment a snapshot frame carries (#3818)", () => {
    expect(
      parseServerFrame(
        frame("snapshot", {
          state: "stopped",
          desired_state: "stopped",
          assigned_worker_id: "w1",
        }),
      ),
    ).toEqual({
      kind: "snapshot",
      state: "stopped",
      desiredState: "stopped",
      assignedWorkerId: "w1",
    });
    expect(
      parseServerFrame(
        frame("snapshot", { state: "stopped", assigned_worker_id: null }),
      ),
    ).toEqual({ kind: "snapshot", state: "stopped", assignedWorkerId: null });
  });

  // A refusal of the stop's final snapshot recorded while its Worker holds
  // the server is published on its own (#4054), and the release clears it
  // with the assignment: a frame carrying only it is not malformed (#4057).
  it("parses a status frame that carries only the final-snapshot refusal (#4057)", () => {
    expect(
      parseServerFrame(frame("status", { final_snapshot_refusal: "busy" })),
    ).toEqual({
      kind: "status",
      finalSnapshotRefusal: "busy",
      detail: "",
      reason: "",
    });
    expect(
      parseServerFrame(
        frame("status", {
          assigned_worker_id: null,
          final_snapshot_refusal: null,
        }),
      ),
    ).toEqual({
      kind: "status",
      assignedWorkerId: null,
      finalSnapshotRefusal: null,
      detail: "",
      reason: "",
    });
  });

  it("leaves the refusal out of a status frame that does not carry it", () => {
    expect(
      parseServerFrame(frame("status", { state: "stopped" })),
    ).not.toHaveProperty("finalSnapshotRefusal");
    expect(
      parseServerFrame(frame("status", { final_snapshot_refusal: 7 })),
    ).toBeNull();
  });

  it("parses the final-snapshot refusal a snapshot frame carries (#4057)", () => {
    expect(
      parseServerFrame(
        frame("snapshot", {
          state: "stopped",
          final_snapshot_refusal: "unavailable",
        }),
      ),
    ).toEqual({
      kind: "snapshot",
      state: "stopped",
      finalSnapshotRefusal: "unavailable",
    });
    expect(
      parseServerFrame(
        frame("snapshot", { state: "stopped", final_snapshot_refusal: null }),
      ),
    ).toEqual({
      kind: "snapshot",
      state: "stopped",
      finalSnapshotRefusal: null,
    });
  });

  it("parses the last crash a snapshot frame carries (#3281)", () => {
    expect(
      parseServerFrame(
        frame("snapshot", {
          state: "crashed",
          crash_reason: "forge_install_out_of_memory",
          crash_detail: "installer ran out of memory",
        }),
      ),
    ).toEqual({
      kind: "snapshot",
      state: "crashed",
      crash: {
        reason: "forge_install_out_of_memory",
        detail: "installer ran out of memory",
      },
    });
    expect(
      parseServerFrame(
        frame("snapshot", {
          state: "running",
          crash_reason: null,
          crash_detail: null,
        }),
      ),
    ).toEqual({
      kind: "snapshot",
      state: "running",
      crash: { reason: null, detail: null },
    });
  });

  it("leaves out a crash the snapshot frame does not carry whole", () => {
    for (const payload of [
      { state: "crashed" },
      { state: "crashed", crash_reason: "forge_install_failed" },
      { state: "crashed", crash_reason: 7, crash_detail: null },
    ]) {
      expect(parseServerFrame(frame("snapshot", payload))).not.toHaveProperty(
        "crash",
      );
    }
  });

  it("drops a snapshot frame without a state", () => {
    expect(parseServerFrame(frame("snapshot", {}))).toBeNull();
    expect(
      parseServerFrame(frame("snapshot", { desired_state: "running" })),
    ).toBeNull();
  });

  it("parses a gap marker (no payload required)", () => {
    expect(parseServerFrame(frame("gap", {}))).toEqual({ kind: "gap" });
    expect(
      parseServerFrame(JSON.stringify({ stream: "gap", ts: "t" })),
    ).toEqual({ kind: "gap" });
  });

  it("drops malformed input and unknown streams", () => {
    expect(parseServerFrame("not json")).toBeNull();
    expect(parseServerFrame(frame("status", { detail: "x" }))).toBeNull();
    expect(parseServerFrame(frame("log", { stream: "stdout" }))).toBeNull();
    expect(parseServerFrame(frame("metrics", { cpu_millis: "x" }))).toBeNull();
    expect(parseServerFrame(frame("other", {}))).toBeNull();
  });
});

describe("ServerEventsClient", () => {
  let restore: () => void;

  beforeEach(() => {
    vi.useFakeTimers();
    restore = installMockWebSocket();
    setAccessToken("tok-1");
  });

  afterEach(() => {
    restore();
    clearAccessToken();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  function makeClient() {
    const onFrame = vi.fn();
    const onOpen = vi.fn();
    const onDown = vi.fn();
    const onAccessLost = vi.fn();
    const client = new ServerEventsClient(
      CID,
      SID,
      { onFrame, onOpen, onDown, onAccessLost },
      () => 0.5,
    );
    return { client, onFrame, onOpen, onDown, onAccessLost };
  }

  it("subscribes to all three streams with the token in subprotocol", () => {
    const { client } = makeClient();
    client.start();
    const ws = MockWebSocket.last();
    expect(ws.url).toContain(`/api/communities/${CID}/servers/${SID}/events`);
    expect(ws.url).toContain("streams=status%2Clog%2Cmetrics");
    expect(ws.url).not.toContain("token=");
    expect(ws.protocols).toEqual(["access_token", "tok-1"]);
    client.close();
  });

  it("routes parsed frames to onFrame and opens", () => {
    const { client, onFrame, onOpen } = makeClient();
    client.start();
    MockWebSocket.last().open();
    expect(onOpen).toHaveBeenCalledTimes(1);

    MockWebSocket.last().message(frame("log", { line: "x", stream: "stdout" }));
    expect(onFrame).toHaveBeenCalledWith({
      kind: "log",
      line: "x",
      stream: "stdout",
    });

    MockWebSocket.last().message(frame("gap", {}));
    expect(onFrame).toHaveBeenCalledWith({ kind: "gap" });
    client.close();
  });

  it("reconnects on loss and reconnects with the fresh token on rotation", () => {
    const { client, onDown } = makeClient();
    client.start();
    const first = MockWebSocket.last();
    first.open();
    first.fail();
    expect(onDown).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(500); // attempt 1: step 1000 * 0.5
    expect(MockWebSocket.instances).toHaveLength(2);

    MockWebSocket.last().open();
    setAccessToken("tok-2"); // rotation
    expect(MockWebSocket.last().protocols).toEqual(["access_token", "tok-2"]);
    client.close();
  });

  it("tears down cleanly: no reconnect after close", () => {
    const { client, onDown } = makeClient();
    client.start();
    const socket = MockWebSocket.last();
    socket.open();
    client.close();
    expect(socket.closed).toBe(true);

    socket.fail();
    vi.advanceTimersByTime(60000);
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(onDown).not.toHaveBeenCalled();
  });

  // A refused handshake is a bare 1006 to the browser (#3313): after three in
  // a row the client reads the server through REST, whose gate is the
  // socket's, and stops once that says the server is gone.
  it("probes the server read after three failed handshakes and stops on its 404", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ status: 404 }), {
          status: 404,
          headers: { "content-type": "application/problem+json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { client, onAccessLost } = makeClient();
    client.start();

    for (let i = 0; i < 3; i++) {
      MockWebSocket.last().fail();
      await vi.advanceTimersByTimeAsync(30000);
    }

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]).toEqual([
      `/api/communities/${CID}/servers/${SID}`,
      expect.objectContaining({ method: "GET" }),
    ]);
    expect(onAccessLost).toHaveBeenCalledTimes(1);
    expect(MockWebSocket.instances).toHaveLength(3);
  });
});
