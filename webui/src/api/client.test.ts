import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken, setAccessToken } from "../auth/tokenStore.ts";
import { ApiError, api, setRefresher } from "./client.ts";

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

    const error = await api.post("/auth/login", { body: "{}" }).catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(401);
    expect(error.reason).toBe("invalid_credentials");
  });

  it("leaves reason undefined for a non-problem body", async () => {
    fetchMock.mockResolvedValue(jsonResponse(500, { message: "boom" }));

    const error = await api.get("/communities").catch((e) => e);

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

    const error = await api.get("/communities").catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(429);
    expect(error.reason).toBe("rate_limited");
  });

  it("synthesizes a typed error from an HTML 502 body, not a SyntaxError", async () => {
    fetchMock.mockResolvedValue(
      htmlResponse(502, "<html><body>502 Bad Gateway</body></html>"),
    );

    const error = await api.get("/communities").catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(502);
    expect(error.reason).toBeUndefined();
  });

  it("fails typed on a non-JSON 2xx body instead of throwing a SyntaxError", async () => {
    fetchMock.mockResolvedValue(
      htmlResponse(200, "<html><body>not json</body></html>"),
    );

    const error = await api.get("/communities").catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(200);
  });
});

describe("request", () => {
  it("attaches the bearer token when present", async () => {
    setAccessToken("tok");
    fetchMock.mockResolvedValue(jsonResponse(200, []));

    await api.get("/communities");

    const headers = fetchMock.mock.calls[0][1].headers as Record<
      string,
      string
    >;
    expect(headers.authorization).toBe("Bearer tok");
  });

  it("omits the authorization header when there is no token", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []));

    await api.get("/communities");

    const headers = fetchMock.mock.calls[0][1].headers as Record<
      string,
      string
    >;
    expect(headers.authorization).toBeUndefined();
  });

  it("sends credentials so the session cookie rides along", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []));

    await api.get("/communities");

    expect(fetchMock.mock.calls[0][1].credentials).toBe("same-origin");
  });
});

describe("transparent 401 refresh", () => {
  it("refreshes once and retries the original request on a 401", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { reason: "x" }))
      .mockResolvedValueOnce(jsonResponse(200, [{ id: "c1" }]));
    const refresher = vi.fn(() => Promise.resolve(true));
    setRefresher(refresher);

    const result = await api.get("/communities");

    expect(refresher).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result).toEqual([{ id: "c1" }]);
  });

  it("does not retry when the refresh fails", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { reason: "x" }));
    const refresher = vi.fn(() => Promise.resolve(false));
    setRefresher(refresher);

    const error = await api.get("/communities").catch((e) => e);

    expect(refresher).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(401);
  });

  it("does not refresh on a 401 from an auth endpoint", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { reason: "x" }));
    const refresher = vi.fn(() => Promise.resolve(true));
    setRefresher(refresher);

    await api.post("/auth/login", { body: "{}" }).catch(() => {});

    expect(refresher).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retries only once: a second 401 after refresh surfaces the error", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { reason: "x" }));
    const refresher = vi.fn(() => Promise.resolve(true));
    setRefresher(refresher);

    const error = await api.get("/communities").catch((e) => e);

    expect(refresher).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(401);
  });
});
