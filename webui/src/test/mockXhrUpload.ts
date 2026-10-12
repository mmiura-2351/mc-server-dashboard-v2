/** Forward XHR uploads through the fetch mock so page tests share one URL/method dispatcher. */

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
