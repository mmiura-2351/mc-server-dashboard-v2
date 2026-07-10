// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken, setAccessToken } from "../auth/tokenStore.ts";
import { ApiError, api, postFormWithProgress, setRefresher } from "./client.ts";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(body === undefined ? "" : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function htmlResponse(status: number, body: string): Response {
  return new Response(body, {
    status,
    headers: { "content-type": "text/html" },
  });
}

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  clearAccessToken();
  // Default refresher: no-op failure, so tests opt into a working refresh.
  setRefresher(() => Promise.resolve(false));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ApiError", () => {
  it("exposes the problem+json reason", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(401, {
        type: "urn:mcsd:error:invalid_credentials",
        title: "Unauthorized",
        status: 401,
        reason: "invalid_credentials",
      }),
    );

    const error = await api
      .post("/api/auth/login", { body: "{}" })
      .catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(401);
    expect(error.reason).toBe("invalid_credentials");
  });

  it("exposes the permission extension member on a 403 denial", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(403, {
        type: "urn:mcsd:error:forbidden",
        title: "Forbidden",
        status: 403,
        reason: "forbidden",
        permission: "server:start",
      }),
    );

    const error = await api.get("/api/communities").catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.reason).toBe("forbidden");
    expect(error.permission).toBe("server:start");
  });

  it("leaves permission undefined when the body omits it", async () => {
    fetchMock.mockResolvedValue(jsonResponse(403, { reason: "forbidden" }));

    const error = await api.get("/api/communities").catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.permission).toBeUndefined();
  });

  it("leaves reason undefined for a non-problem body", async () => {
    fetchMock.mockResolvedValue(jsonResponse(500, { message: "boom" }));

    const error = await api.get("/api/communities").catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.reason).toBeUndefined();
  });

  it("still parses a problem+json error body for the reason", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ reason: "rate_limited" }), {
        status: 429,
        headers: { "content-type": "application/problem+json" },
      }),
    );

    const error = await api.get("/api/communities").catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(429);
    expect(error.reason).toBe("rate_limited");
  });

  it("synthesizes a typed error from an HTML 502 body, not a SyntaxError", async () => {
    fetchMock.mockResolvedValue(
      htmlResponse(502, "<html><body>502 Bad Gateway</body></html>"),
    );

    const error = await api.get("/api/communities").catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(502);
    expect(error.reason).toBeUndefined();
  });

  it("fails typed on a non-JSON 2xx body instead of throwing a SyntaxError", async () => {
    fetchMock.mockResolvedValue(
      htmlResponse(200, "<html><body>not json</body></html>"),
    );

    const error = await api.get("/api/communities").catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(200);
  });
});

describe("request", () => {
  it("attaches the bearer token when present", async () => {
    setAccessToken("tok");
    fetchMock.mockResolvedValue(jsonResponse(200, []));

    await api.get("/api/communities");

    const headers = fetchMock.mock.calls[0][1].headers as Record<
      string,
      string
    >;
    expect(headers.authorization).toBe("Bearer tok");
  });

  it("omits the authorization header when there is no token", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []));

    await api.get("/api/communities");

    const headers = fetchMock.mock.calls[0][1].headers as Record<
      string,
      string
    >;
    expect(headers.authorization).toBeUndefined();
  });

  it("sends credentials so the session cookie rides along", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []));

    await api.get("/api/communities");

    expect(fetchMock.mock.calls[0][1].credentials).toBe("same-origin");
  });
});

describe("request cancellation", () => {
  it("forwards the caller's abort signal to fetch", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []));
    const controller = new AbortController();

    await api.get("/api/communities", { signal: controller.signal });

    expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal);
  });

  it("rejects with the abort error, not an ApiError, when aborted", async () => {
    // Emulate fetch's abort contract: reject with an AbortError DOMException
    // once the signal fires.
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_, reject) => {
          init.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    );
    const controller = new AbortController();

    const promise = api
      .get("/api/communities", { signal: controller.signal })
      .catch((e) => e);
    controller.abort();

    const error = await promise;
    expect(error).not.toBeInstanceOf(ApiError);
    expect(error.name).toBe("AbortError");
  });
});

describe("transparent 401 refresh", () => {
  it("refreshes once and retries the original request on a 401", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { reason: "x" }))
      .mockResolvedValueOnce(jsonResponse(200, [{ id: "c1" }]));
    const refresher = vi.fn(() => Promise.resolve(true));
    setRefresher(refresher);

    const result = await api.get("/api/communities");

    expect(refresher).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result).toEqual([{ id: "c1" }]);
  });

  it("does not retry when the refresh fails", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { reason: "x" }));
    const refresher = vi.fn(() => Promise.resolve(false));
    setRefresher(refresher);

    const error = await api.get("/api/communities").catch((e) => e);

    expect(refresher).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(401);
  });

  it("does not refresh on a 401 from an auth endpoint", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { reason: "x" }));
    const refresher = vi.fn(() => Promise.resolve(true));
    setRefresher(refresher);

    await api.post("/api/auth/login", { body: "{}" }).catch(() => {});

    expect(refresher).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retries only once: a second 401 after refresh surfaces the error", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { reason: "x" }));
    const refresher = vi.fn(() => Promise.resolve(true));
    setRefresher(refresher);

    const error = await api.get("/api/communities").catch((e) => e);

    expect(refresher).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(401);
  });
});

// A minimal XMLHttpRequest test double: progress feedback needs the real upload
// progress events, which fetch cannot surface, so postFormWithProgress is built
// on XHR (issue #1207). The double records each constructed instance, captures
// the request, and lets a test drive the response and the upload progress.
class MockXHR {
  static instances: MockXHR[] = [];

  static last(): MockXHR {
    const xhr = MockXHR.instances.at(-1);
    if (xhr === undefined) {
      throw new Error("no MockXHR constructed");
    }
    return xhr;
  }

  upload = { onprogress: null as ((e: ProgressEvent) => void) | null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  withCredentials = false;
  status = 0;
  responseText = "";
  private headers: Record<string, string> = {};
  private responseHeaders: Record<string, string> = {
    "content-type": "application/json",
  };
  method = "";
  url = "";
  body: unknown = null;

  constructor() {
    MockXHR.instances.push(this);
  }

  open(method: string, url: string): void {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(name: string, value: string): void {
    this.headers[name.toLowerCase()] = value;
  }

  getResponseHeader(name: string): string | null {
    return this.responseHeaders[name.toLowerCase()] ?? null;
  }

  send(body: unknown): void {
    this.body = body;
  }

  // --- test drivers --------------------------------------------------------

  reqHeaders(): Record<string, string> {
    return this.headers;
  }

  progress(loaded: number, total: number): void {
    this.upload.onprogress?.({
      lengthComputable: true,
      loaded,
      total,
    } as ProgressEvent);
  }

  /** Resolve the request with a JSON body and status. */
  respond(status: number, body: unknown, contentType = "application/json") {
    this.status = status;
    this.responseHeaders["content-type"] = contentType;
    this.responseText = body === undefined ? "" : JSON.stringify(body);
    this.onload?.();
  }

  /** Simulate a network-level failure. */
  fail(): void {
    this.onerror?.();
  }
}

describe("postFormWithProgress", () => {
  beforeEach(() => {
    MockXHR.instances = [];
    vi.stubGlobal("XMLHttpRequest", MockXHR);
  });

  it("POSTs the FormData body and resolves the parsed JSON on success", async () => {
    const form = new FormData();
    form.append("file", new Blob(["x"]));
    const promise = postFormWithProgress("/api/resource-packs", form);
    const xhr = MockXHR.last();

    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe("/api/resource-packs");
    expect(xhr.body).toBe(form);

    xhr.respond(201, { id: "rp1" });
    await expect(promise).resolves.toEqual({ id: "rp1" });
  });

  it("attaches the bearer token and sends credentials", async () => {
    setAccessToken("tok");
    const promise = postFormWithProgress("/api/resource-packs", new FormData());
    const xhr = MockXHR.last();

    expect(xhr.reqHeaders().authorization).toBe("Bearer tok");
    expect(xhr.withCredentials).toBe(true);

    xhr.respond(201, { id: "rp1" });
    await promise;
  });

  it("reports upload progress through the callback", async () => {
    const onProgress = vi.fn();
    const promise = postFormWithProgress(
      "/api/resource-packs",
      new FormData(),
      onProgress,
    );
    const xhr = MockXHR.last();

    xhr.progress(50, 200);
    xhr.progress(200, 200);

    expect(onProgress).toHaveBeenNthCalledWith(1, 50, 200);
    expect(onProgress).toHaveBeenNthCalledWith(2, 200, 200);

    xhr.respond(201, { id: "rp1" });
    await promise;
  });

  it("rejects with a typed ApiError carrying the problem reason", async () => {
    const promise = postFormWithProgress(
      "/api/resource-packs",
      new FormData(),
    ).catch((e) => e);
    MockXHR.last().respond(
      413,
      { reason: "file_too_large" },
      "application/problem+json",
    );

    const error = await promise;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(413);
    expect(error.reason).toBe("file_too_large");
  });

  it("rejects with a typed ApiError on a network failure", async () => {
    const promise = postFormWithProgress(
      "/api/resource-packs",
      new FormData(),
    ).catch((e) => e);
    MockXHR.last().fail();

    const error = await promise;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
  });

  it("refreshes once and retries the upload on a 401", async () => {
    const refresher = vi.fn(() => Promise.resolve(true));
    setRefresher(refresher);

    const promise = postFormWithProgress("/api/resource-packs", new FormData());
    MockXHR.last().respond(401, { reason: "x" });
    // Let the refresh microtask run, then the retried request resolves.
    await Promise.resolve();
    await Promise.resolve();
    MockXHR.last().respond(201, { id: "rp1" });

    await expect(promise).resolves.toEqual({ id: "rp1" });
    expect(refresher).toHaveBeenCalledTimes(1);
    expect(MockXHR.instances).toHaveLength(2);
  });

  it("does not retry when the refresh fails", async () => {
    const refresher = vi.fn(() => Promise.resolve(false));
    setRefresher(refresher);

    const promise = postFormWithProgress(
      "/api/resource-packs",
      new FormData(),
    ).catch((e) => e);
    MockXHR.last().respond(401, { reason: "x" });

    const error = await promise;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(401);
    expect(MockXHR.instances).toHaveLength(1);
  });
});
