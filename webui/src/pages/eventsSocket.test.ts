import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { clearAccessToken, setAccessToken } from "../auth/tokenStore.ts";
import { installMockWebSocket, MockWebSocket } from "../test/mockWebSocket.ts";
import { EventsSocketClient } from "./eventsSocket.ts";

// The close policy of the shared socket core (#3313): which closes stop the
// client, and how repeated failed handshakes are told apart from a network
// failure. The URL and frames are the wrappers' concern (their own tests).
describe("EventsSocketClient close policy", () => {
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

  function makeClient(probeAccess: () => Promise<boolean> = async () => true) {
    const callbacks = {
      onMessage: vi.fn(),
      onOpen: vi.fn(),
      onDown: vi.fn(),
      onAccessLost: vi.fn(),
      probeAccess: vi.fn(probeAccess),
    };
    const client = new EventsSocketClient(
      () => "ws://test/events",
      callbacks,
      () => 0.5,
    );
    client.start();
    return { client, ...callbacks };
  }

  /** Fail `count` handshakes in a row: each socket closes before opening. */
  async function failHandshakes(count: number) {
    for (let i = 0; i < count; i++) {
      MockWebSocket.last().fail();
      await vi.advanceTimersByTimeAsync(30000); // past any backoff step
    }
  }

  it.each([4403, 4404])(
    "stops reconnecting on a mid-stream %i and reports the lost access",
    async (code) => {
      const { client, onAccessLost, onDown, probeAccess } = makeClient();
      MockWebSocket.last().open();

      MockWebSocket.last().serverClose(code);
      await vi.advanceTimersByTimeAsync(120000);

      expect(onAccessLost).toHaveBeenCalledTimes(1);
      expect(onDown).not.toHaveBeenCalled();
      expect(MockWebSocket.instances).toHaveLength(1);
      expect(probeAccess).not.toHaveBeenCalled();
      client.close();
    },
  );

  it("reconnects on the backoff after a 1013 (the API's database is unavailable)", async () => {
    const { client, onAccessLost, onDown } = makeClient();
    MockWebSocket.last().open();

    MockWebSocket.last().serverClose(1013);
    expect(onDown).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(499);
    expect(MockWebSocket.instances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1); // attempt 1: 1000 * 0.5
    expect(MockWebSocket.instances).toHaveLength(2);
    expect(MockWebSocket.last().protocols).toEqual(["access_token", "tok-1"]);
    expect(onAccessLost).not.toHaveBeenCalled();
    client.close();
  });

  it("probes REST only after three failed handshakes in a row", async () => {
    const { client, probeAccess } = makeClient();

    await failHandshakes(2);
    expect(probeAccess).not.toHaveBeenCalled();
    await failHandshakes(1);
    expect(probeAccess).toHaveBeenCalledTimes(1);
    // The count restarts after a probe: the next one is three failures away.
    await failHandshakes(2);
    expect(probeAccess).toHaveBeenCalledTimes(1);
    await failHandshakes(1);
    expect(probeAccess).toHaveBeenCalledTimes(2);
    client.close();
  });

  it("restarts the handshake-failure count on a successful open", async () => {
    const { client, probeAccess } = makeClient();

    await failHandshakes(2);
    MockWebSocket.last().open();
    MockWebSocket.last().fail(); // a drop after opening is no failed handshake
    await vi.advanceTimersByTimeAsync(30000);
    await failHandshakes(2);

    expect(probeAccess).not.toHaveBeenCalled();
    client.close();
  });

  it.each([403, 404])(
    "stops reconnecting when the probe answers %i (the handshake is refused)",
    async (status) => {
      const { client, onAccessLost } = makeClient(async () => {
        throw new ApiError(status, undefined);
      });

      await failHandshakes(3);
      await vi.advanceTimersByTimeAsync(120000);

      expect(onAccessLost).toHaveBeenCalledTimes(1);
      // The three refused handshakes, and no reconnect after the probe.
      expect(MockWebSocket.instances).toHaveLength(3);
      client.close();
    },
  );

  // A probe can answer 200 and still say the stream is refused: the community
  // stream's permissions read does, for a member without `server:read`.
  it("stops reconnecting when the probe resolves that access is gone", async () => {
    const { client, onAccessLost } = makeClient(async () => false);

    await failHandshakes(3);
    await vi.advanceTimersByTimeAsync(120000);

    expect(onAccessLost).toHaveBeenCalledTimes(1);
    expect(MockWebSocket.instances).toHaveLength(3);
    client.close();
  });

  // A probe answers about the moment it was sent: a denial that lands after a
  // later handshake opened is stale and must not stop the recovered socket.
  it.each([403, 404])(
    "ignores a %i probe answer that lands after the socket recovered",
    async (status) => {
      let deny: () => void = () => {};
      const { client, onAccessLost } = makeClient(
        () =>
          new Promise((_resolve, reject) => {
            deny = () => reject(new ApiError(status, undefined));
          }),
      );

      await failHandshakes(3); // the probe is pending; the backoff reconnected
      MockWebSocket.last().open();
      deny();
      await vi.advanceTimersByTimeAsync(0);

      expect(onAccessLost).not.toHaveBeenCalled();
      expect(MockWebSocket.last().closed).toBe(false);
      // Still live: a rotation reconnects it with the fresh token.
      setAccessToken("tok-2");
      expect(MockWebSocket.last().protocols).toEqual(["access_token", "tok-2"]);
      client.close();
    },
  );

  it.each([
    ["succeeds", async () => true],
    [
      "fails on the network",
      async () => {
        throw new TypeError("Failed to fetch");
      },
    ],
    [
      "answers 503",
      async () => {
        throw new ApiError(503, undefined);
      },
    ],
  ])("keeps the backoff when the probe %s", async (_label, probe) => {
    const { client, onAccessLost, probeAccess } = makeClient(probe);

    await failHandshakes(3);

    expect(probeAccess).toHaveBeenCalledTimes(1);
    expect(onAccessLost).not.toHaveBeenCalled();
    // The three failed handshakes, then the backoff reconnect.
    expect(MockWebSocket.instances).toHaveLength(4);
    client.close();
  });
});
