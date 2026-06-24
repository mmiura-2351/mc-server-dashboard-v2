import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "../auth/tokenStore.ts";
import { t } from "../i18n/index.ts";
import { renderApp } from "../test/render.tsx";

// No-community empty state (#584). Driven through the real router + providers via
// renderApp; the fetch mock returns an empty community list so Landing falls
// through to the empty state, and a per-test current-user controls the admin
// branch.

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function tokenResponse(): Response {
  return jsonResponse({
    access_token: "fresh",
    token_type: "bearer",
  });
}

const fetchMock = vi.fn();

const ADMIN = {
  id: "u1",
  username: "admin",
  email: "admin@example.com",
  is_platform_admin: true,
};
const MEMBER = { ...ADMIN, is_platform_admin: false };

// Sign in with the given user and no communities, so Landing lands on the empty
// state. The bootstrap refresh and any unmatched URL fall through to a token.
function signedInWithNoCommunities(user: typeof ADMIN) {
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url === "/api/users/me") return Promise.resolve(jsonResponse(user));
    if (url === "/api/communities") return Promise.resolve(jsonResponse([]));
    return Promise.resolve(tokenResponse());
  });
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  clearAccessToken();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("no-community empty state", () => {
  it("explains the no-community state instead of the bare placeholder", async () => {
    signedInWithNoCommunities(MEMBER);

    renderApp({ path: "/" });

    expect(
      await screen.findByRole("heading", { name: t("noCommunity.title") }),
    ).toBeInTheDocument();
    expect(screen.getByText(t("noCommunity.body"))).toBeInTheDocument();
    // The generic placeholder copy must be gone.
    expect(screen.queryByText(t("page.placeholder"))).not.toBeInTheDocument();
  });

  it("offers the create-community action to a platform admin", async () => {
    signedInWithNoCommunities(ADMIN);

    renderApp({ path: "/" });

    const cta = await screen.findByRole("link", {
      name: t("noCommunity.adminCta"),
    });
    expect(cta).toHaveAttribute("href", "/admin/communities");
  });

  it("does not offer the create-community action to a non-admin", async () => {
    signedInWithNoCommunities(MEMBER);

    renderApp({ path: "/" });

    await screen.findByRole("heading", { name: t("noCommunity.title") });
    expect(
      screen.queryByRole("link", { name: t("noCommunity.adminCta") }),
    ).not.toBeInTheDocument();
  });
});
