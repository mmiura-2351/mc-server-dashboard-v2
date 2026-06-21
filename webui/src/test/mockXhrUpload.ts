/**
 * Mock `XMLHttpRequest` that routes uploads through an existing `fetch` mock.
 *
 * `postFormWithProgress` (issue #1207) is built on XHR, which jsdom does not
 * usefully implement, so a page test that dispatches its API by URL + method
 * through a `fetch` mock would never see the upload. This double forwards the
 * XHR request to that same `fetch` mock and copies the resolved `Response` back
 * onto the XHR fields the client reads (status, responseText, content-type), so
 * the existing dispatch table covers uploads too. Install it for the duration of
 * a test and restore it after.
 */

type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

class MockUploadXHR {
  upload = { onprogress: null as ((e: ProgressEvent) => void) | null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  withCredentials = false;
  status = 0;
  responseText = "";
  private method = "";
  private url = "";
  private contentType = "application/json";
  private headers: Record<string, string> = {};

  constructor(private readonly fetchMock: FetchLike) {}

  open(method: string, url: string): void {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(name: string, value: string): void {
    this.headers[name.toLowerCase()] = value;
  }

  getResponseHeader(name: string): string | null {
    return name.toLowerCase() === "content-type" ? this.contentType : null;
  }

  send(body: FormData): void {
    void this.dispatch(body);
  }

  private async dispatch(body: FormData): Promise<void> {
    try {
      const response = await this.fetchMock(this.url, {
        method: this.method,
        body,
        headers: this.headers,
      });
      this.status = response.status;
      this.contentType =
        response.headers.get("content-type") ?? "application/json";
      this.responseText = await response.text();
      // Emit a single completed-progress event so the bar exercises its path.
      this.upload.onprogress?.({
        lengthComputable: true,
        loaded: 1,
        total: 1,
      } as ProgressEvent);
      this.onload?.();
    } catch {
      this.onerror?.();
    }
  }
}

/** Install the upload XHR double over the global; returns a restore function. */
export function installMockXhrUpload(fetchMock: FetchLike): () => void {
  const original = globalThis.XMLHttpRequest;
  // biome-ignore lint/suspicious/noExplicitAny: test double over the DOM global.
  (globalThis as any).XMLHttpRequest = class extends MockUploadXHR {
    constructor() {
      super(fetchMock);
    }
  };
  return () => {
    globalThis.XMLHttpRequest = original;
  };
}
