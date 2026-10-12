/** Record constructed sockets and expose server-side event drivers; install and restore the global per test. */

export class MockWebSocket {
  static instances: MockWebSocket[] = [];

  static reset(): void {
    MockWebSocket.instances = [];
  }

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

  open(): void {
    this.onopen?.();
  }

  message(data: unknown): void {
    this.onmessage?.({
      data: typeof data === "string" ? data : JSON.stringify(data),
    });
  }

  fail(): void {
    this.onerror?.();
    this.onclose?.({ code: 1006 });
  }

  serverClose(code: number): void {
    this.onclose?.({ code });
  }
}

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
