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
    ).toEqual({ kind: "status", state: "running", detail: "ok" });
  });

  it("defaults a missing status detail to empty", () => {
    expect(parseServerFrame(frame("status", { state: "running" }))).toEqual({
      kind: "status",
      state: "running",
      detail: "",
    });
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
    vi.useRealTimers();
  });

  function makeClient() {
    const onFrame = vi.fn();
    const onOpen = vi.fn();
    const onDown = vi.fn();
    const client = new ServerEventsClient(
      CID,
      SID,
      { onFrame, onOpen, onDown },
      () => 0.5,
    );
    return { client, onFrame, onOpen, onDown };
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
});
