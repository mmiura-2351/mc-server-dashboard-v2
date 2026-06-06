import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App.tsx";
import { SessionProvider } from "./auth/SessionProvider.tsx";
import { clearAccessToken } from "./auth/tokenStore.ts";
import { t } from "./i18n/index.ts";

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

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <SessionProvider>
        <App />
      </SessionProvider>
    </MemoryRouter>,
  );
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
    fetchMock.mockResolvedValue(tokenResponse());

    renderAt("/communities/demo");

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: t("page.dashboard") }),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("link", { name: t("nav.dashboard") }),
    ).toBeInTheDocument();
  });

  it("redirects signed-in users away from /login to the dashboard", async () => {
    fetchMock.mockResolvedValue(tokenResponse());

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
