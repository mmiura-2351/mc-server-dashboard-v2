import { fireEvent, screen, waitFor } from "@testing-library/react";
import { useRef } from "react";
import { useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "./auth/tokenStore.ts";
import { t } from "./i18n/index.ts";
import { useActiveCommunity } from "./permissions/ActiveCommunityProvider.tsx";
import { renderApp } from "./test/render.tsx";

// Suspend an otherwise unused route to test the shell's loading boundary without affecting dashboard cases.
vi.mock("./pages/AccountPage.tsx", () => {
  const pending = new Promise<void>(() => {});
  return {
    AccountPage: () => {
      throw pending;
    },
  };
});

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

// Expose the URL without mounting a guarded page to observe deep-link redirects.
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

    // Both the switcher and landing page show this error, so multiple matches are expected.
    const errors = await screen.findAllByText(t("shell.communitiesError"));
    expect(errors.length).toBeGreaterThanOrEqual(1);
    const retries = screen.getAllByRole("button", {
      name: t("shell.communitiesRetry"),
    });
    expect(retries.length).toBeGreaterThanOrEqual(1);
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

    const retryButtons = await screen.findAllByRole("button", {
      name: t("shell.communitiesRetry"),
    });

    fireEvent.click(retryButtons[0]);

    await waitFor(() =>
      expect(screen.getByTestId("url").textContent).toBe("/communities/alpha"),
    );
  });
});

describe("App route guards", () => {
  it("shows a neutral loading state while the session bootstraps", async () => {
    // Release the bootstrap request before the next test so shared session state cannot retain it.
    let release: (r: Response) => void = () => {};
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          release = resolve;
        }),
    );

    renderAt("/communities/demo");

    expect(screen.getByText(t("auth.loading"))).toBeInTheDocument();
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

    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: t("nav.dashboard") }),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText(t("auth.loading"))).toBeInTheDocument();
  });

  it("never flashes NoCommunityPage while the default community resolves", async () => {
    signedInWith([{ id: "alpha", name: "Alpha" }]);

    // Record every render to catch a transient no-community frame after the list has loaded.
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
