import { fireEvent, screen, waitFor } from "@testing-library/react";
import { useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "./auth/tokenStore.ts";
import { t } from "./i18n/index.ts";
import { renderApp } from "./test/render.tsx";

// The bootstrap refresh decides signed-in vs signed-out. A 200 token response
// signs in; a 401 signs out; a pending promise keeps it "bootstrapping".
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
    if (url === "/communities") {
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
      if (url === "/auth/refresh") {
        return Promise.resolve(new Response("", { status: 401 }));
      }
      if (url === "/communities") {
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
