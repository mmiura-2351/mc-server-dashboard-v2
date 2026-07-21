import { fireEvent, screen, waitFor } from "@testing-library/react";
import { useRef } from "react";
import { useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "./auth/tokenStore.ts";
import { t } from "./i18n/index.ts";
import { useActiveCommunity } from "./permissions/ActiveCommunityProvider.tsx";
import { renderApp } from "./test/render.tsx";

// Suspend the account route indefinitely so the lazy-chunk loading frame is
// observable: the component throws a never-resolving promise on render, exactly
// as React.lazy does while its chunk is in flight. Lets the test assert which
// boundary catches the suspension — the shell's inner one, not an outer one.
// (Account is a shell route no other case in this file visits, so the broad
// module mock does not disturb the dashboard-rendering tests.)
vi.mock("./pages/AccountPage.tsx", () => {
  const pending = new Promise<void>(() => {});
  return {
    AccountPage: () => {
      throw pending;
    },
  };
});

// The bootstrap refresh decides signed-in vs signed-out. A 200 token response
// signs in; a 401 signs out; a pending promise keeps it "bootstrapping".
function tokenResponse(): Response {
  return new Response(
    JSON.stringify({
      access_token: "fresh",
      token_type: "bearer",
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

// Route the bootstrap refresh and the community list (and its per-community
// permission fetch) so the shell can resolve an active community. The shell's
// switcher reads `GET /communities` via the shared ActiveCommunityProvider.
function signedInWith(communities: Array<{ id: string; name: string }>) {
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url === "/api/communities") {
      return Promise.resolve(jsonResponse(communities));
    }
    if (url.endsWith("/me/permissions")) {
      return Promise.resolve(jsonResponse({}));
    }
    if (url.endsWith("/servers")) {
      return Promise.resolve(jsonResponse([]));
    }
    return Promise.resolve(tokenResponse());
  });
}

// Surfaces the live URL so the deep-link redirect can be asserted without
// depending on a guarded page's content (DashboardPage is owned elsewhere).
function LocationProbe() {
  const { pathname, search } = useLocation();
  return <span data-testid="url">{`${pathname}${search}`}</span>;
}

function renderAt(path: string) {
  renderApp({ path, extras: <LocationProbe /> });
}

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  clearAccessToken();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Landing communities error state", () => {
  it("shows an error with a retry button when GET /communities fails", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/communities") {
        return Promise.resolve(
          new Response("", {
            status: 500,
            statusText: "Internal Server Error",
          }),
        );
      }
      return Promise.resolve(tokenResponse());
    });

    renderAt("/");

    // The error message appears in the Landing content area (the CommunitySwitcher
    // also shows the error, so multiple elements match — use findAllByText).
    const errors = await screen.findAllByText(t("shell.communitiesError"));
    expect(errors.length).toBeGreaterThanOrEqual(1);
    // At least one retry button is present.
    const retries = screen.getAllByRole("button", {
      name: t("shell.communitiesRetry"),
    });
    expect(retries.length).toBeGreaterThanOrEqual(1);
    // The loading indicator must not be showing.
    expect(screen.queryByText(t("auth.loading"))).not.toBeInTheDocument();
  });

  it("retry button re-fetches communities after an error", async () => {
    let callCount = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/communities") {
        callCount++;
        if (callCount <= 1) {
          return Promise.resolve(new Response("", { status: 500 }));
        }
        return Promise.resolve(jsonResponse([{ id: "alpha", name: "Alpha" }]));
      }
      if (url.endsWith("/me/permissions")) {
        return Promise.resolve(jsonResponse({}));
      }
      if (url.endsWith("/servers")) {
        return Promise.resolve(jsonResponse([]));
      }
      return Promise.resolve(tokenResponse());
    });

    renderApp({ path: "/", extras: <LocationProbe /> });

    // Wait for the error state (multiple retry buttons: switcher + landing).
    const retryButtons = await screen.findAllByRole("button", {
      name: t("shell.communitiesRetry"),
    });

    // Click the first retry — the second attempt succeeds and redirects to the dashboard.
    fireEvent.click(retryButtons[0]);

    await waitFor(() =>
      expect(screen.getByTestId("url").textContent).toBe("/communities/alpha"),
    );
  });
});

describe("App route guards", () => {
  it("shows a neutral loading state while the session bootstraps", async () => {
    // Hold the bootstrap refresh in flight, then release it so the shared
    // single-flight promise in the session module resolves and does not leak
    // a pending state into the next test.
    let release: (r: Response) => void = () => {};
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          release = resolve;
        }),
    );

    renderAt("/communities/demo");

    expect(screen.getByText(t("auth.loading"))).toBeInTheDocument();
    // No redirect flash to /login while bootstrapping.
    expect(
      screen.queryByRole("button", { name: t("login.submit") }),
    ).not.toBeInTheDocument();

    release(new Response("", { status: 401 }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: t("login.submit") }),
      ).toBeInTheDocument(),
    );
  });

  it("redirects signed-out users from a shell route to /login", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 401 }));

    renderAt("/communities/demo");

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: t("login.submit") }),
      ).toBeInTheDocument(),
    );
    // The guarded dashboard never renders.
    expect(
      screen.queryByRole("heading", { name: t("page.dashboard") }),
    ).not.toBeInTheDocument();
  });

  it("renders the shell for signed-in users", async () => {
    signedInWith([{ id: "alpha", name: "Alpha" }]);

    renderAt("/communities/alpha");

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: t("page.dashboard") }),
      ).toBeInTheDocument(),
    );
    // The community-scoped nav resolves once the community list arrives.
    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: t("nav.dashboard") }),
      ).toBeInTheDocument(),
    );
  });

  it("redirects signed-in users away from /login to the dashboard", async () => {
    signedInWith([{ id: "alpha", name: "Alpha" }]);

    renderAt("/login");

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: t("page.dashboard") }),
      ).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("button", { name: t("login.submit") }),
    ).not.toBeInTheDocument();
  });

  it("returns to the requested deep link (path + query) after login", async () => {
    // Bootstrap signed-out, but let /auth/login succeed and the community list
    // resolve so the shell can render the originally requested route.
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/auth/session") {
        return Promise.resolve(new Response("", { status: 401 }));
      }
      if (url === "/api/communities") {
        return Promise.resolve(jsonResponse([{ id: "demo", name: "Demo" }]));
      }
      if (url.endsWith("/me/permissions")) {
        return Promise.resolve(jsonResponse({}));
      }
      return Promise.resolve(tokenResponse());
    });

    renderAt("/communities/demo/servers/s1?tab=logs");

    // Bounced to /login with the deep link stashed in router state.
    const button = await screen.findByRole("button", {
      name: t("login.submit"),
    });
    expect(screen.getByTestId("url").textContent).toBe("/login");

    fireEvent.change(screen.getByLabelText(t("auth.fieldUsername")), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText(t("auth.fieldPassword")), {
      target: { value: "a-password" },
    });
    fireEvent.click(button);

    await waitFor(() =>
      expect(screen.getByTestId("url").textContent).toBe(
        "/communities/demo/servers/s1?tab=logs",
      ),
    );
  });

  it("keeps the shell mounted while a lazy route chunk loads", async () => {
    signedInWith([{ id: "alpha", name: "Alpha" }]);

    renderAt("/account");

    // The account route suspends (mocked above), so the content area shows the
    // loading fallback — but the shell chrome must stay mounted. With the
    // Suspense boundary inside AppShell around <Outlet>, the sidebar nav renders
    // alongside the fallback instead of being torn down to the bare fallback.
    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: t("nav.dashboard") }),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText(t("auth.loading"))).toBeInTheDocument();
  });

  it("never flashes NoCommunityPage while the default community resolves", async () => {
    signedInWith([{ id: "alpha", name: "Alpha" }]);

    // A probe that records every (communityId, communities) pair it observes
    // across renders. If the provider exposes a frame where communities are
    // loaded but communityId is still null, this probe captures it (issue #2014).
    function CommunityProbe() {
      const { communityId, communities } = useActiveCommunity();
      const log = useRef<Array<{ id: string | null; len: number | undefined }>>(
        [],
      );
      log.current.push({ id: communityId, len: communities?.length });
      return (
        <span data-testid="community-log">{JSON.stringify(log.current)}</span>
      );
    }

    renderApp({
      path: "/",
      extras: (
        <>
          <LocationProbe />
          <CommunityProbe />
        </>
      ),
    });

    await waitFor(() =>
      expect(screen.getByTestId("url").textContent).toBe("/communities/alpha"),
    );

    // Parse the render log and verify no frame had communities loaded (len > 0)
    // while communityId was null — that would be the NoCommunityPage flash.
    const log = JSON.parse(
      screen.getByTestId("community-log").textContent ?? "[]",
    );
    const flash = log.some(
      (entry: { id: string | null; len: number | undefined }) =>
        entry.len !== undefined && entry.len > 0 && entry.id === null,
    );
    expect(flash).toBe(false);
  });

  it("renders the login page for signed-out users without shell chrome", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 401 }));

    renderAt("/login");

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: t("login.submit") }),
      ).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("link", { name: t("nav.dashboard") }),
    ).not.toBeInTheDocument();
  });
});
