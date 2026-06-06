import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetForTesting as resetClientForTesting } from "../api/client.ts";
import { SessionProvider, useSession } from "./SessionProvider.tsx";
import { resetForTesting as resetSessionForTesting } from "./session.ts";
import { clearAccessToken } from "./tokenStore.ts";

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

function StatusProbe() {
  const { status, logout } = useSession();
  const location = useLocation();
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="path">{location.pathname}</span>
      <button type="button" onClick={() => logout()}>
        logout
      </button>
    </div>
  );
}

function renderSession() {
  return render(
    <MemoryRouter initialEntries={["/account"]}>
      <SessionProvider>
        <Routes>
          <Route path="*" element={<StatusProbe />} />
        </Routes>
      </SessionProvider>
    </MemoryRouter>,
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
});
