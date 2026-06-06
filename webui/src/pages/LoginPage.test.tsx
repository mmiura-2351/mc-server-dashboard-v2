import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionProvider } from "../auth/SessionProvider.tsx";
import { clearAccessToken, getAccessToken } from "../auth/tokenStore.ts";
import { t } from "../i18n/index.ts";
import { LoginPage } from "./LoginPage.tsx";

function PathProbe() {
  return <span data-testid="path">{useLocation().pathname}</span>;
}

function renderLogin() {
  render(
    <MemoryRouter initialEntries={["/login"]}>
      <SessionProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="*" element={<PathProbe />} />
        </Routes>
      </SessionProvider>
    </MemoryRouter>,
  );
}

const fetchMock = vi.fn();

// Route the bootstrap refresh to signed-out so the login page renders; the
// per-test login response is queued via mockResolvedValueOnce after mount.
function bootstrapSignedOut() {
  fetchMock.mockImplementation(async (url: string) => {
    if (url === "/auth/refresh") {
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

  it("stores the token and lands on the dashboard on success", async () => {
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
      expect(screen.getByTestId("path")).toHaveTextContent("/communities/demo"),
    );
    expect(getAccessToken()).toBe("issued");
  });
});
