// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  hardLogout,
  logout,
  type RefreshResult,
  refreshForRetry,
  refreshSession,
  resetForTesting,
  restoreSession,
  setHardLogoutHandler,
  signIn,
} from "./session.ts";
import {
  clearAccessToken,
  getAccessToken,
  setAccessToken,
} from "./tokenStore.ts";

function tokenResponse(accessToken = "fresh"): Response {
  return new Response(
    JSON.stringify({
      access_token: accessToken,
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

    const result = await refreshSession();

    expect(result).toEqual({ status: "ok" });
    expect(getAccessToken()).toBe("fresh");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/auth/refresh");
    expect(init.credentials).toBe("same-origin");
    expect(init.body).toBe("{}");
  });

  it("reports auth-rejected on a 401 and stores no token", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 401 }));

    const result = await refreshSession();

    expect(result).toEqual({ status: "auth-rejected" });
    expect(getAccessToken()).toBeNull();
  });

  it("reports auth-rejected on a 403", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 403 }));

    const result = await refreshSession();

    expect(result).toEqual({ status: "auth-rejected" });
  });

  it("reports transient when the network call throws", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));

    const result = await refreshSession();

    expect(result).toEqual({ status: "transient" });
  });

  it("reports transient on a 5xx response", async () => {
    fetchMock.mockResolvedValue(new Response("Bad Gateway", { status: 502 }));

    const result = await refreshSession();

    expect(result).toEqual({ status: "transient" });
  });

  it("reports transient on a 200 with an invalid JSON body", async () => {
    fetchMock.mockResolvedValue(new Response("not json", { status: 200 }));

    const result = await refreshSession();

    expect(result).toEqual({ status: "transient" });
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
    expect(results).toEqual([
      { status: "ok" },
      { status: "ok" },
      { status: "ok" },
    ] satisfies RefreshResult[]);
  });

  it("starts a fresh refresh after the previous one settles", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(tokenResponse()));

    await refreshSession();
    await refreshSession();

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

function accessTokenResponse(accessToken = "fresh"): Response {
  return new Response(
    JSON.stringify({ access_token: accessToken, token_type: "bearer" }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

describe("restoreSession", () => {
  it("posts /api/auth/session (the non-rotating bootstrap path)", async () => {
    fetchMock.mockResolvedValue(accessTokenResponse());

    const result = await restoreSession();

    expect(result).toBe("signed-in");
    expect(getAccessToken()).toBe("fresh");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/auth/session");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
  });

  it("reports signed out on a 401 and stores no token", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 401 }));

    const result = await restoreSession();

    expect(result).toBe("signed-out");
    expect(getAccessToken()).toBeNull();
  });

  it("treats the 401 probe as the no-session signal without logging an error (#641)", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 401 }));
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

    // The 401 on /login and after logout is the documented "no session" state;
    // the app must resolve signed-out silently and never surface it as an
    // application error (the browser's native "Failed to load resource ... 401"
    // line is separate and not suppressible from JS).
    await expect(restoreSession()).resolves.toBe("signed-out");
    expect(errorSpy).not.toHaveBeenCalled();
    expect(warnSpy).not.toHaveBeenCalled();

    errorSpy.mockRestore();
    warnSpy.mockRestore();
  });

  it("reports signed out when the network call throws", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));

    const result = await restoreSession();

    expect(result).toBe("signed-out");
  });

  it("resolves signed out on a 200 with an invalid JSON body", async () => {
    fetchMock.mockResolvedValue(new Response("not json", { status: 200 }));

    const result = await restoreSession();

    expect(result).toBe("signed-out");
    expect(getAccessToken()).toBeNull();
  });

  it("never rotates: repeated restores each just POST /api/auth/session", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(accessTokenResponse()));

    await restoreSession();
    await restoreSession();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (const [url] of fetchMock.mock.calls) {
      expect(url).toBe("/api/auth/session");
    }
  });
});

describe("refreshForRetry", () => {
  it("hard-logs-out on an auth-definitive 401", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 401 }));
    const onLogout = vi.fn();
    setHardLogoutHandler(onLogout);
    setAccessToken("stale");

    const ok = await refreshForRetry();

    expect(ok).toBe(false);
    expect(getAccessToken()).toBeNull();
    expect(onLogout).toHaveBeenCalledWith("expired");
  });

  it("hard-logs-out on an auth-definitive 403", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 403 }));
    const onLogout = vi.fn();
    setHardLogoutHandler(onLogout);
    setAccessToken("stale");

    const ok = await refreshForRetry();

    expect(ok).toBe(false);
    expect(getAccessToken()).toBeNull();
    expect(onLogout).toHaveBeenCalledWith("expired");
  });

  it("does not hard-logout on a network error (transient)", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    const onLogout = vi.fn();
    setHardLogoutHandler(onLogout);
    setAccessToken("stale");

    const ok = await refreshForRetry();

    expect(ok).toBe(false);
    expect(onLogout).not.toHaveBeenCalled();
  });

  it("does not hard-logout on a 5xx response (transient)", async () => {
    fetchMock.mockResolvedValue(new Response("Bad Gateway", { status: 502 }));
    const onLogout = vi.fn();
    setHardLogoutHandler(onLogout);
    setAccessToken("stale");

    const ok = await refreshForRetry();

    expect(ok).toBe(false);
    expect(onLogout).not.toHaveBeenCalled();
  });

  it("does not hard-logout on a 200 with an invalid JSON body (transient)", async () => {
    fetchMock.mockResolvedValue(new Response("not json", { status: 200 }));
    const onLogout = vi.fn();
    setHardLogoutHandler(onLogout);
    setAccessToken("stale");

    const ok = await refreshForRetry();

    expect(ok).toBe(false);
    expect(onLogout).not.toHaveBeenCalled();
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

describe("signIn", () => {
  it("adopts the access token", () => {
    signIn("session-B");

    expect(getAccessToken()).toBe("session-B");
  });
});

/**
 * Hold every fetch pending and hand back its resolvers in call order, so a test
 * can let an authentication response land after the session has changed.
 */
function holdFetches(): ((response: Response) => void)[] {
  const resolvers: ((response: Response) => void)[] = [];
  fetchMock.mockImplementation(
    () =>
      new Promise<Response>((resolve) => {
        resolvers.push(resolve);
      }),
  );
  return resolvers;
}

// A response begun under one session must never act on a later one: logout and
// sign-in start a new session, and anything still in flight from before is
// discarded on arrival (#3224).
describe("late authentication responses", () => {
  it("a refresh that lands after logout does not repopulate the token", async () => {
    const pending = holdFetches();
    setAccessToken("session-A");
    const refresh = refreshSession();

    hardLogout();
    pending[0](tokenResponse("late-session-A"));

    expect(await refresh).toEqual({ status: "superseded" });
    expect(getAccessToken()).toBeNull();
  });

  it("a refresh that lands after another user signs in does not replace their token", async () => {
    const pending = holdFetches();
    setAccessToken("session-A");
    const refresh = refreshSession();

    hardLogout();
    signIn("session-B");
    pending[0](tokenResponse("late-session-A"));

    expect(await refresh).toEqual({ status: "superseded" });
    expect(getAccessToken()).toBe("session-B");
  });

  it("a bootstrap that lands after logout does not repopulate the token", async () => {
    const pending = holdFetches();
    const restore = restoreSession();

    hardLogout();
    pending[0](accessTokenResponse("late-session-A"));

    expect(await restore).toBe("superseded");
    expect(getAccessToken()).toBeNull();
  });

  it("a bootstrap that lands after a sign-in does not replace the new token", async () => {
    const pending = holdFetches();
    const restore = restoreSession();

    signIn("session-B");
    pending[0](accessTokenResponse("late-session-A"));

    expect(await restore).toBe("superseded");
    expect(getAccessToken()).toBe("session-B");
  });

  it("a rejected bootstrap that lands after a sign-in reports superseded, not signed-out", async () => {
    const pending = holdFetches();
    const restore = restoreSession();

    signIn("session-B");
    pending[0](new Response("", { status: 401 }));

    expect(await restore).toBe("superseded");
  });

  it("a rejected refresh that lands after another user signs in does not log them out", async () => {
    const pending = holdFetches();
    const onLogout = vi.fn();
    setHardLogoutHandler(onLogout);
    setAccessToken("session-A");
    const retry = refreshForRetry();

    hardLogout();
    signIn("session-B");
    onLogout.mockClear();
    pending[0](new Response("", { status: 401 }));

    expect(await retry).toBe(false);
    expect(onLogout).not.toHaveBeenCalled();
    expect(getAccessToken()).toBe("session-B");
  });

  it("a successful refresh that lands after another user signs in asks for no retry", async () => {
    const pending = holdFetches();
    setAccessToken("session-A");
    const retry = refreshForRetry();

    hardLogout();
    signIn("session-B");
    pending[0](tokenResponse("late-session-A"));

    // A retry would replay user A's request as user B.
    expect(await retry).toBe(false);
  });

  it("a refresh in the new session does not join the old session's in-flight one", async () => {
    const pending = holdFetches();
    setAccessToken("session-A");
    const stale = refreshSession();

    hardLogout();
    signIn("session-B");
    const current = refreshSession();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    pending[1](tokenResponse("rotated-B"));
    expect(await current).toEqual({ status: "ok" });
    pending[0](tokenResponse("late-session-A"));
    expect(await stale).toEqual({ status: "superseded" });
    expect(getAccessToken()).toBe("rotated-B");
  });

  it("the old session's refresh settling keeps the new session single-flight", async () => {
    const pending = holdFetches();
    setAccessToken("session-A");
    const stale = refreshSession();
    hardLogout();
    signIn("session-B");
    const current = refreshSession();

    pending[0](tokenResponse("late-session-A"));
    await stale;
    const joined = refreshSession();

    expect(joined).toBe(current);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    pending[1](tokenResponse("rotated-B"));
    await current;
  });
});
