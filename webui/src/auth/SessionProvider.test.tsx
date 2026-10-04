import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetForTesting as resetClientForTesting } from "../api/client.ts";
import { SessionProvider, useSession } from "./SessionProvider.tsx";
import {
  hardLogout,
  refreshForRetry,
  resetForTesting as resetSessionForTesting,
} from "./session.ts";
import {
  clearAccessToken,
  getAccessToken,
  getAuthEpoch,
} from "./tokenStore.ts";

function tokenResponse(accessToken = "fresh"): Response {
  return new Response(
    JSON.stringify({
      access_token: accessToken,
      token_type: "bearer",
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

function StatusProbe() {
  const { status, signIn, logout } = useSession();
  const location = useLocation();
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="path">{location.pathname}</span>
      <span data-testid="search">{location.search}</span>
      <button type="button" onClick={() => logout()}>
        logout
      </button>
      <button type="button" onClick={() => signIn("session-B")}>
        sign in as B
      </button>
    </div>
  );
}

function renderSession(
  queryClient = new QueryClient(),
  initialEntry = "/account",
) {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <SessionProvider>
          <Routes>
            <Route path="*" element={<StatusProbe />} />
          </Routes>
        </SessionProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  clearAccessToken();
  // The session core keeps module-level singletons (the injected hard-logout
  // handler / refresher and the in-flight refresh). A handler left by a prior
  // case is bound to an unmounted render, so a later logout navigates a stale
  // router and the path never reaches /login. Reset them per case to isolate.
  resetSessionForTesting();
  resetClientForTesting();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SessionProvider bootstrap", () => {
  it("starts bootstrapping, then signs in when the cookie refresh succeeds", async () => {
    let resolveFetch: (r: Response) => void = () => {};
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );

    renderSession();
    expect(screen.getByTestId("status")).toHaveTextContent("bootstrapping");

    resolveFetch(tokenResponse());
    await waitFor(() =>
      expect(screen.getByTestId("status")).toHaveTextContent("signed-in"),
    );
    // Bootstrap uses the non-rotating restore endpoint, never the rotating
    // /api/auth/refresh, so a page load can no longer rotate the cookie (#512).
    expect(fetchMock.mock.calls[0][0]).toBe("/api/auth/session");
  });

  it("signs out without navigating when the cookie refresh fails", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 401 }));

    renderSession();

    await waitFor(() =>
      expect(screen.getByTestId("status")).toHaveTextContent("signed-out"),
    );
    // Bootstrap leaves routing to the guards (#410); it does not redirect.
    expect(screen.getByTestId("path")).toHaveTextContent("/account");
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

/** Let `n` microtasks run. */
async function microtasks(n: number): Promise<void> {
  for (let i = 0; i < n; i++) {
    await Promise.resolve();
  }
}

/**
 * Every microtask count at which a session switch can land after the bootstrap
 * response arrived: before its result is computed, between computing it and
 * the provider applying it, and after.
 */
const SWITCH_POINTS = Array.from({ length: 12 }, (_, i) => i);

// The bootstrap response arrives first and the session changes only afterwards,
// while the computed result is still travelling to the provider (#3224 review).
describe("SessionProvider bootstrap result consumed after the session changed", () => {
  it.each(SWITCH_POINTS)(
    "a signed-out result never overrides a sign-in made %i microtasks after it arrived",
    async (n) => {
      const pending = holdFetches();
      renderSession();
      pending[0](new Response("", { status: 401 }));

      await microtasks(n);
      act(() => screen.getByRole("button", { name: "sign in as B" }).click());
      await act(async () => {});

      expect(screen.getByTestId("status")).toHaveTextContent("signed-in");
      expect(getAccessToken()).toBe("session-B");
    },
  );

  it.each(SWITCH_POINTS)(
    "a signed-in result never overrides a logout made %i microtasks after it arrived",
    async (n) => {
      const pending = holdFetches();
      renderSession();
      pending[0](tokenResponse("session-A"));

      await microtasks(n);
      act(() => hardLogout());
      await act(async () => {});

      expect(screen.getByTestId("status")).toHaveTextContent("signed-out");
      expect(getAccessToken()).toBeNull();
    },
  );

  it.each(SWITCH_POINTS)(
    "a signed-in result keeps a sign-in made %i microtasks after it arrived",
    async (n) => {
      const pending = holdFetches();
      renderSession();
      pending[0](tokenResponse("session-A"));

      await microtasks(n);
      act(() => screen.getByRole("button", { name: "sign in as B" }).click());
      await act(async () => {});

      expect(screen.getByTestId("status")).toHaveTextContent("signed-in");
      expect(getAccessToken()).toBe("session-B");
    },
  );
});

// A sign-in starts a new session; an authentication response still in flight
// from before must not change what the provider reports or holds (#3224).
describe("SessionProvider sign-in during an in-flight request", () => {
  it("keeps the new sign-in when a bootstrap rejection lands late", async () => {
    const pending = holdFetches();
    renderSession();

    act(() => screen.getByRole("button", { name: "sign in as B" }).click());
    await act(async () => pending[0](new Response("", { status: 401 })));

    expect(screen.getByTestId("status")).toHaveTextContent("signed-in");
    expect(getAccessToken()).toBe("session-B");
  });

  it("keeps the new user's token when a bootstrap success lands late", async () => {
    const pending = holdFetches();
    renderSession();

    act(() => screen.getByRole("button", { name: "sign in as B" }).click());
    await act(async () => pending[0](tokenResponse("late-session-A")));

    expect(screen.getByTestId("status")).toHaveTextContent("signed-in");
    expect(getAccessToken()).toBe("session-B");
  });

  it("keeps the new user's token when an earlier refresh lands late", async () => {
    const pending = holdFetches();
    renderSession();
    await act(async () => pending[0](tokenResponse("session-A")));
    const retry = refreshForRetry(getAuthEpoch());

    act(() => screen.getByRole("button", { name: "sign in as B" }).click());
    await act(async () => pending[1](tokenResponse("late-session-A")));

    expect(await retry).toBe(false);
    expect(getAccessToken()).toBe("session-B");
  });
});

describe("SessionProvider logout", () => {
  it("resets to signed-out and navigates to /login", async () => {
    fetchMock.mockResolvedValueOnce(tokenResponse());
    renderSession();
    await waitFor(() =>
      expect(screen.getByTestId("status")).toHaveTextContent("signed-in"),
    );

    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    screen.getByRole("button", { name: "logout" }).click();

    await waitFor(() =>
      expect(screen.getByTestId("status")).toHaveTextContent("signed-out"),
    );
    await waitFor(() =>
      expect(screen.getByTestId("path")).toHaveTextContent("/login"),
    );
  });

  it("clears the query cache so the next user sees no stale data", async () => {
    const queryClient = new QueryClient();
    // Seed the cache as if the previous user's queries had populated it.
    queryClient.setQueryData(["users", "me"], { username: "alice" });
    queryClient.setQueryData(["communities"], [{ id: "c1", name: "Alpha" }]);

    fetchMock.mockResolvedValueOnce(tokenResponse());
    renderSession(queryClient);
    await waitFor(() =>
      expect(screen.getByTestId("status")).toHaveTextContent("signed-in"),
    );

    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    screen.getByRole("button", { name: "logout" }).click();

    await waitFor(() =>
      expect(screen.getByTestId("status")).toHaveTextContent("signed-out"),
    );
    // The previous user's cached queries are gone before the next sign-in,
    // so no stale data renders for the next account (#532).
    expect(queryClient.getQueryData(["users", "me"])).toBeUndefined();
    expect(queryClient.getQueryData(["communities"])).toBeUndefined();
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });

  it("captures the current location into next on an involuntary 401 expiry", async () => {
    fetchMock.mockResolvedValueOnce(tokenResponse());
    renderSession(new QueryClient(), "/communities/c1?tab=logs");
    await waitFor(() =>
      expect(screen.getByTestId("status")).toHaveTextContent("signed-in"),
    );

    // A transparent refresh that 401s is involuntary: it should land on /login
    // carrying reason=expired and the return-to location as next.
    fetchMock.mockResolvedValueOnce(new Response("", { status: 401 }));
    await refreshForRetry(getAuthEpoch());

    await waitFor(() =>
      expect(screen.getByTestId("path")).toHaveTextContent("/login"),
    );
    const search = screen.getByTestId("search").textContent ?? "";
    const params = new URLSearchParams(search);
    expect(params.get("reason")).toBe("expired");
    expect(params.get("next")).toBe("/communities/c1?tab=logs");
  });

  it("does not set next or a reason on a deliberate user logout", async () => {
    fetchMock.mockResolvedValueOnce(tokenResponse());
    renderSession(new QueryClient(), "/communities/c1?tab=logs");
    await waitFor(() =>
      expect(screen.getByTestId("status")).toHaveTextContent("signed-in"),
    );

    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    screen.getByRole("button", { name: "logout" }).click();

    await waitFor(() =>
      expect(screen.getByTestId("path")).toHaveTextContent("/login"),
    );
    // A deliberate logout lands on a clean /login: no reason, no next.
    expect(screen.getByTestId("search").textContent).toBe("");
  });

  it("clears the query cache on a hard logout from a failed refresh", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(["users", "me"], { username: "alice" });

    fetchMock.mockResolvedValueOnce(tokenResponse());
    renderSession(queryClient);
    await waitFor(() =>
      expect(screen.getByTestId("status")).toHaveTextContent("signed-in"),
    );

    // A transparent refresh that fails drives a hard logout (no API logout call).
    fetchMock.mockResolvedValueOnce(new Response("", { status: 401 }));
    await refreshForRetry(getAuthEpoch());

    await waitFor(() =>
      expect(screen.getByTestId("status")).toHaveTextContent("signed-out"),
    );
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });
});
