/**
 * Minimal mock `WebSocket` for the community-events tests.
 *
 * Only what the client wrapper touches: the four handler slots, `close()`, and
 * test helpers to drive open / message / close. Each construction is recorded
 * on {@link MockWebSocket.instances} so a test can grab the live socket and
 * simulate the server side. Install it over `globalThis.WebSocket` for the
 * duration of a test and restore it after.
 */

export class MockWebSocket {
  static instances: MockWebSocket[] = [];

  static reset(): void {
    MockWebSocket.instances = [];
  }

  /** The most recently constructed socket (the one the client is using). */
  static last(): MockWebSocket {
    const socket = MockWebSocket.instances.at(-1);
    if (socket === undefined) {
      throw new Error("no MockWebSocket has been constructed");
    }
    return socket;
  }

  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(
    readonly url: string,
    readonly protocols?: string | string[],
  ) {
    MockWebSocket.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  // --- test drivers (the "server side") -----------------------------------

  open(): void {
    this.onopen?.();
  }

  message(data: unknown): void {
    this.onmessage?.({
      data: typeof data === "string" ? data : JSON.stringify(data),
    });
  }

  /** Simulate a network failure: error then an abnormal (1006) close. */
  fail(): void {
    this.onerror?.();
    this.onclose?.({ code: 1006 });
  }

  /** Simulate the server closing the socket with an application `code`. */
  serverClose(code: number): void {
    this.onclose?.({ code });
  }
}

/** Install the mock over the global `WebSocket`; returns a restore function. */
export function installMockWebSocket(): () => void {
  const original = globalThis.WebSocket;
  MockWebSocket.reset();
  // biome-ignore lint/suspicious/noExplicitAny: test double over the DOM global.
  (globalThis as any).WebSocket = MockWebSocket;
  return () => {
    // biome-ignore lint/suspicious/noExplicitAny: restore the DOM global.
    (globalThis as any).WebSocket = original;
  };
}
