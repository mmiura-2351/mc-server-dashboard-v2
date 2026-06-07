import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  hardLogout,
  logout,
  refreshForRetry,
  refreshSession,
  resetForTesting,
  setHardLogoutHandler,
} from "./session.ts";
import {
  clearAccessToken,
  getAccessToken,
  setAccessToken,
} from "./tokenStore.ts";

function tokenResponse(): Response {
  return new Response(
    JSON.stringify({
      access_token: "fresh",
      refresh_token: "ignored",
      token_type: "bearer",
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  clearAccessToken();
  resetForTesting();
  setHardLogoutHandler(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("refreshSession", () => {
  it("stores the rotated access token on a 200", async () => {
    fetchMock.mockResolvedValue(tokenResponse());

    const ok = await refreshSession();

    expect(ok).toBe(true);
    expect(getAccessToken()).toBe("fresh");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/auth/refresh");
    expect(init.credentials).toBe("same-origin");
    expect(init.body).toBe("{}");
  });

  it("reports signed out on a 401 and stores no token", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 401 }));

    const ok = await refreshSession();

    expect(ok).toBe(false);
    expect(getAccessToken()).toBeNull();
  });

  it("reports signed out when the network call throws", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));

    const ok = await refreshSession();

    expect(ok).toBe(false);
  });

  it("resolves signed out on a 200 with an invalid JSON body", async () => {
    fetchMock.mockResolvedValue(new Response("not json", { status: 200 }));

    const ok = await refreshSession();

    expect(ok).toBe(false);
    expect(getAccessToken()).toBeNull();
  });

  it("is single-flight: N concurrent calls share one refresh", async () => {
    let resolveFetch: (r: Response) => void = () => {};
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );

    const calls = [refreshSession(), refreshSession(), refreshSession()];
    resolveFetch(tokenResponse());
    const results = await Promise.all(calls);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(results).toEqual([true, true, true]);
  });

  it("starts a fresh refresh after the previous one settles", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(tokenResponse()));

    await refreshSession();
    await refreshSession();

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("refreshForRetry", () => {
  it("hard-logs-out when the refresh fails", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 401 }));
    const onLogout = vi.fn();
    setHardLogoutHandler(onLogout);
    setAccessToken("stale");

    const ok = await refreshForRetry();

    expect(ok).toBe(false);
    expect(getAccessToken()).toBeNull();
    expect(onLogout).toHaveBeenCalledTimes(1);
  });

  it("hard-logs-out on a 200 with an invalid JSON body", async () => {
    fetchMock.mockResolvedValue(new Response("not json", { status: 200 }));
    const onLogout = vi.fn();
    setHardLogoutHandler(onLogout);
    setAccessToken("stale");

    const ok = await refreshForRetry();

    expect(ok).toBe(false);
    expect(getAccessToken()).toBeNull();
    expect(onLogout).toHaveBeenCalledTimes(1);
  });

  it("does not log out when the refresh succeeds", async () => {
    fetchMock.mockResolvedValue(tokenResponse());
    const onLogout = vi.fn();
    setHardLogoutHandler(onLogout);

    const ok = await refreshForRetry();

    expect(ok).toBe(true);
    expect(onLogout).not.toHaveBeenCalled();
  });
});

describe("logout", () => {
  it("calls /auth/logout, drops the token, and resets state", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    const onLogout = vi.fn();
    setHardLogoutHandler(onLogout);
    setAccessToken("stale");

    await logout();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/auth/logout");
    expect(getAccessToken()).toBeNull();
    expect(onLogout).toHaveBeenCalledTimes(1);
  });

  it("still resets local state when the logout call fails", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    const onLogout = vi.fn();
    setHardLogoutHandler(onLogout);
    setAccessToken("stale");

    await logout();

    expect(getAccessToken()).toBeNull();
    expect(onLogout).toHaveBeenCalledTimes(1);
  });
});

describe("hardLogout", () => {
  it("clears the token and runs the handler without an API call", () => {
    const onLogout = vi.fn();
    setHardLogoutHandler(onLogout);
    setAccessToken("stale");

    hardLogout();

    expect(getAccessToken()).toBeNull();
    expect(onLogout).toHaveBeenCalledTimes(1);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
