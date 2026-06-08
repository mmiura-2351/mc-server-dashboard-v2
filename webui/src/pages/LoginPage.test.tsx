import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionProvider } from "../auth/SessionProvider.tsx";
import { clearAccessToken, getAccessToken } from "../auth/tokenStore.ts";
import { t } from "../i18n/index.ts";
import { LoginPage } from "./LoginPage.tsx";

function PathProbe() {
  const { pathname, search } = useLocation();
  return <span data-testid="path">{`${pathname}${search}`}</span>;
}

function renderLogin(
  fromState?: {
    from: { pathname: string; search: string };
  },
  loginSearch = "",
) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter
        initialEntries={[
          {
            pathname: "/login",
            search: loginSearch,
            state: fromState ?? null,
          },
        ]}
      >
        <SessionProvider>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="*" element={<PathProbe />} />
          </Routes>
        </SessionProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const fetchMock = vi.fn();

// Route the bootstrap refresh to signed-out so the login page renders; the
// per-test login response is queued via mockResolvedValueOnce after mount.
function bootstrapSignedOut() {
  fetchMock.mockImplementation(async (url: string) => {
    if (url === "/api/auth/refresh") {
      return new Response("", { status: 401 });
    }
    throw new Error(`unexpected fetch ${url}`);
  });
}

async function submitCredentials() {
  const button = await screen.findByRole("button", { name: t("login.submit") });
  fireEvent.change(screen.getByLabelText(t("auth.fieldUsername")), {
    target: { value: "alice" },
  });
  fireEvent.change(screen.getByLabelText(t("auth.fieldPassword")), {
    target: { value: "a-password" },
  });
  fireEvent.click(button);
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  clearAccessToken();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("LoginPage", () => {
  it("shows a single generic error on a 401, leaking no detail", async () => {
    bootstrapSignedOut();
    renderLogin();

    // The login POST is rejected with the uniform 401 (AUTH_API.md 1).
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ reason: "invalid_credentials", status: 401 }),
        {
          status: 401,
          headers: { "content-type": "application/problem+json" },
        },
      ),
    );
    await submitCredentials();

    expect(
      await screen.findByText(t("login.invalidCredentials")),
    ).toBeInTheDocument();
    // Still on the login page; no token stored.
    expect(getAccessToken()).toBeNull();
  });

  it("stores the token and lands on the post-login landing on success", async () => {
    bootstrapSignedOut();
    renderLogin();

    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          access_token: "issued",
          refresh_token: "ignored",
          token_type: "bearer",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    await submitCredentials();

    await waitFor(() =>
      expect(screen.getByTestId("path").textContent).toBe("/"),
    );
    expect(getAccessToken()).toBe("issued");
  });

  it("returns to the stashed deep link (path + query) after login", async () => {
    bootstrapSignedOut();
    renderLogin({
      from: {
        pathname: "/api/communities/demo/servers/s1",
        search: "?tab=logs",
      },
    });

    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          access_token: "issued",
          refresh_token: "ignored",
          token_type: "bearer",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    await submitCredentials();

    await waitFor(() =>
      expect(screen.getByTestId("path").textContent).toBe(
        "/api/communities/demo/servers/s1?tab=logs",
      ),
    );
  });

  it("shows the session-expired notice only when arriving via expiry", async () => {
    bootstrapSignedOut();
    renderLogin(undefined, "?reason=expired");

    expect(
      await screen.findByText(t("login.sessionExpired")),
    ).toBeInTheDocument();
  });

  it("shows no expiry notice on a normal first visit to /login", async () => {
    bootstrapSignedOut();
    renderLogin();

    await screen.findByRole("button", { name: t("login.submit") });
    expect(screen.queryByText(t("login.sessionExpired"))).toBeNull();
  });

  it("exposes a top-level heading naming the app (a11y; #647)", async () => {
    bootstrapSignedOut();
    renderLogin();

    expect(
      await screen.findByRole("heading", { level: 1, name: t("app.title") }),
    ).toBeInTheDocument();
  });

  it("returns to a valid next param after login", async () => {
    bootstrapSignedOut();
    renderLogin(
      undefined,
      "?reason=expired&next=%2Fcommunities%2Fc1%3Ftab%3Dlogs%23h",
    );

    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          access_token: "issued",
          refresh_token: "ignored",
          token_type: "bearer",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    await submitCredentials();

    await waitFor(() =>
      expect(screen.getByTestId("path").textContent).toBe(
        "/communities/c1?tab=logs",
      ),
    );
  });

  it("falls back to the landing for an invalid next param (open redirect)", async () => {
    bootstrapSignedOut();
    renderLogin(undefined, "?reason=expired&next=%2F%2Fevil.com");

    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          access_token: "issued",
          refresh_token: "ignored",
          token_type: "bearer",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    await submitCredentials();

    await waitFor(() =>
      expect(screen.getByTestId("path").textContent).toBe("/"),
    );
  });
});
