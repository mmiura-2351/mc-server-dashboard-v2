// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  refreshForRetry,
  resetForTesting as resetSession,
  setHardLogoutHandler,
} from "../auth/session.ts";
import {
  advanceAuthEpoch,
  clearAccessToken,
  getAccessToken,
  getAuthEpoch,
  setAccessToken,
} from "../auth/tokenStore.ts";
import {
  ApiError,
  api,
  isUploadAbortError,
  postFormWithProgress,
  setRefresher,
} from "./client.ts";

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

  it("exposes the key extension member on a 422 platform_managed_key refusal", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, {
        type: "urn:mcsd:error:platform-managed-key",
        title: "Unprocessable Content",
        status: 422,
        reason: "platform_managed_key",
        key: "rcon.password",
      }),
    );

    const error = await api.get("/api/communities").catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.reason).toBe("platform_managed_key");
    expect(error.key).toBe("rcon.password");
  });

  it("leaves key undefined when the body omits it", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, { reason: "platform_managed_path" }),
    );

    const error = await api.get("/api/communities").catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.key).toBeUndefined();
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

  // A 401 belongs to the session the request was sent under; the refresh and
  // the retry must not act for a session that replaced it (#3224).
  it("hands the refresher the epoch the request was sent in", async () => {
    let respond: (r: Response) => void = () => {};
    fetchMock.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          respond = resolve;
        }),
    );
    const refresher = vi.fn(() => Promise.resolve(false));
    setRefresher(refresher);
    const sentEpoch = getAuthEpoch();

    const pending = api.get("/api/communities").catch(() => {});
    advanceAuthEpoch();
    respond(jsonResponse(401, { reason: "x" }));
    await pending;

    expect(refresher).toHaveBeenCalledWith(sentEpoch);
  });

  it("does not retry when the session changes after the refresh succeeds", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { reason: "x" }));
    setRefresher(
      vi.fn(async () => {
        // The session switches after the refresh result is computed but
        // before the client acts on it.
        queueMicrotask(advanceAuthEpoch);
        return true;
      }),
    );

    const error = await api.get("/api/communities").catch((e) => e);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(error.status).toBe(401);
  });
});

// The API holds a body past 64 KiB that carries no valid credential -- an
// expired access token among them -- to a 413 with this reason, in place of the
// route's 401 (SECURITY.md Section 5). The client treats it as the 401 it
// stands for.
const CREDENTIAL_REQUIRED = { reason: "credential_required_for_body" };

function problemResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/problem+json" },
  });
}

describe("transparent refresh on a body refused for want of a credential", () => {
  const largeBody = JSON.stringify({ name: "x".repeat(70 * 1024) });

  it("refreshes once and sends the same body again", async () => {
    setAccessToken("stale");
    fetchMock
      .mockResolvedValueOnce(problemResponse(413, CREDENTIAL_REQUIRED))
      .mockResolvedValueOnce(jsonResponse(201, { id: "c1" }));
    const refresher = vi.fn(async () => {
      setAccessToken("fresh");
      return true;
    });
    setRefresher(refresher);

    const result = await api.post("/api/communities", { body: largeBody });

    expect(result).toEqual({ id: "c1" });
    expect(refresher).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [, retried] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(retried.body).toBe(largeBody);
    expect((retried.headers as Record<string, string>).authorization).toBe(
      "Bearer fresh",
    );
  });

  it("retries only once: the same refusal after a refresh surfaces", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(problemResponse(413, CREDENTIAL_REQUIRED)),
    );
    const refresher = vi.fn(() => Promise.resolve(true));
    setRefresher(refresher);

    const error = await api
      .post("/api/communities", { body: largeBody })
      .catch((e) => e);

    expect(refresher).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(413);
    expect(error.reason).toBe("credential_required_for_body");
  });

  it("does not refresh on a body past the route's own cap", async () => {
    fetchMock.mockResolvedValue(
      problemResponse(413, { reason: "request_too_large" }),
    );
    const refresher = vi.fn(() => Promise.resolve(true));
    setRefresher(refresher);

    const error = await api
      .post("/api/communities", { body: largeBody })
      .catch((e) => e);

    expect(refresher).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(error.reason).toBe("request_too_large");
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
  onabort: (() => void) | null = null;
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

  /** Simulate an abort. */
  abort(): void {
    this.onabort?.();
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

  it("hands the refresher the epoch the upload was sent in", async () => {
    const refresher = vi.fn(() => Promise.resolve(false));
    setRefresher(refresher);
    const sentEpoch = getAuthEpoch();

    const promise = postFormWithProgress(
      "/api/resource-packs",
      new FormData(),
    ).catch(() => {});
    advanceAuthEpoch();
    MockXHR.last().respond(401, { reason: "x" });
    await promise;

    expect(refresher).toHaveBeenCalledWith(sentEpoch);
  });

  it("does not retry the upload when the session changes after the refresh succeeds", async () => {
    setRefresher(
      vi.fn(async () => {
        queueMicrotask(advanceAuthEpoch);
        return true;
      }),
    );

    const promise = postFormWithProgress(
      "/api/resource-packs",
      new FormData(),
    ).catch((e) => e);
    MockXHR.last().respond(401, { reason: "x" });

    const error = await promise;
    expect(error.status).toBe(401);
    expect(MockXHR.instances).toHaveLength(1);
  });

  it("rejects with an abort error when the signal fires", async () => {
    const controller = new AbortController();
    const promise = postFormWithProgress(
      "/api/resource-packs",
      new FormData(),
      undefined,
      controller.signal,
    ).catch((e) => e);

    // Aborting the signal should trigger xhr.abort(), which fires onabort.
    controller.abort();

    const error = await promise;
    expect(isUploadAbortError(error)).toBe(true);
    expect(error).not.toBeInstanceOf(ApiError);
  });

  it("rejects immediately when the signal is already aborted", async () => {
    const controller = new AbortController();
    controller.abort();

    const error = await postFormWithProgress(
      "/api/resource-packs",
      new FormData(),
      undefined,
      controller.signal,
    ).catch((e) => e);

    expect(isUploadAbortError(error)).toBe(true);
    // No XHR should have been constructed.
    expect(MockXHR.instances).toHaveLength(0);
  });
});

describe("postFormWithProgress on an upload refused for want of a credential", () => {
  beforeEach(() => {
    MockXHR.instances = [];
    vi.stubGlobal("XMLHttpRequest", MockXHR);
  });

  afterEach(() => {
    resetSession();
  });

  function largeUpload(): FormData {
    const form = new FormData();
    form.append("file", new Blob(["x".repeat(70 * 1024)]));
    return form;
  }

  it("refreshes an expired token and sends the upload again", async () => {
    setAccessToken("expired");
    setRefresher(async () => {
      setAccessToken("fresh");
      return true;
    });
    const form = largeUpload();

    const promise = postFormWithProgress("/api/resource-packs", form);
    MockXHR.last().respond(
      413,
      CREDENTIAL_REQUIRED,
      "application/problem+json",
    );
    await vi.waitFor(() => expect(MockXHR.instances).toHaveLength(2));
    const retried = MockXHR.last();
    retried.respond(201, { id: "rp1" });

    await expect(promise).resolves.toEqual({ id: "rp1" });
    expect(retried.body).toBe(form);
    expect(retried.reqHeaders().authorization).toBe("Bearer fresh");
  });

  it("logs out when the refresh is refused, without sending it again", async () => {
    // The real session refresher: the refresh cookie is rejected too.
    setAccessToken("expired");
    const onLogout = vi.fn();
    setHardLogoutHandler(onLogout);
    setRefresher(refreshForRetry);
    fetchMock.mockResolvedValue(new Response("", { status: 401 }));

    const promise = postFormWithProgress(
      "/api/resource-packs",
      largeUpload(),
    ).catch((e) => e);
    MockXHR.last().respond(
      413,
      CREDENTIAL_REQUIRED,
      "application/problem+json",
    );
    const error = await promise;

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/auth/refresh",
      expect.anything(),
    );
    expect(onLogout).toHaveBeenCalledWith("expired");
    expect(getAccessToken()).toBeNull();
    expect(MockXHR.instances).toHaveLength(1);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.reason).toBe("credential_required_for_body");
  });

  it("does not refresh on an upload past the route's own cap", async () => {
    const refresher = vi.fn(() => Promise.resolve(true));
    setRefresher(refresher);

    const promise = postFormWithProgress(
      "/api/resource-packs",
      largeUpload(),
    ).catch((e) => e);
    MockXHR.last().respond(
      413,
      { reason: "too_large" },
      "application/problem+json",
    );

    const error = await promise;
    expect(refresher).not.toHaveBeenCalled();
    expect(error.reason).toBe("too_large");
  });
});

describe("isUploadAbortError", () => {
  it("returns true for a DOMException with name AbortError", () => {
    expect(isUploadAbortError(new DOMException("aborted", "AbortError"))).toBe(
      true,
    );
  });

  it("returns false for an ApiError", () => {
    expect(isUploadAbortError(new ApiError(0, undefined))).toBe(false);
  });

  it("returns false for a plain Error", () => {
    expect(isUploadAbortError(new Error("oops"))).toBe(false);
  });
});
