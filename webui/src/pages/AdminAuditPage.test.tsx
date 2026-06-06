import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "../auth/tokenStore.ts";
import { t } from "../i18n/index.ts";
import { renderApp } from "../test/render.tsx";

// Global admin Audit page (#479). Driven through the real router and providers
// via renderApp; a fetch mock dispatches on URL so a single test can stand up
// /users/me, the community list, and the platform-admin global `/audit`
// endpoint (path carries the query string).

const COMMUNITY = { id: "11111111-1111-1111-1111-111111111111", name: "Alpha" };

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function tokenResponse(): Response {
  return jsonResponse({
    access_token: "fresh",
    refresh_token: "ignored",
    token_type: "bearer",
  });
}

function record(over: Record<string, unknown> = {}) {
  return {
    id: "a1",
    operation: "server:start",
    outcome: "success",
    created_at: "2026-06-06T12:00:00Z",
    actor_id: "u1",
    community_id: COMMUNITY.id,
    target_type: "server",
    target_id: "s1",
    ...over,
  };
}

const fetchMock = vi.fn();

const ADMIN = {
  id: "u1",
  username: "admin",
  email: "admin@example.com",
  is_platform_admin: true,
};
const MEMBER = { ...ADMIN, is_platform_admin: false };

// Wire the fetch mock for a signed-in user with the given admin flag. The audit
// endpoint returns the given records; the bootstrap refresh and any unmatched
// URL fall through to a token response.
function signedInAs(user: typeof ADMIN, records: unknown[]) {
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url === "/users/me") return Promise.resolve(jsonResponse(user));
    if (url === "/communities")
      return Promise.resolve(jsonResponse([COMMUNITY]));
    if (url.endsWith("/me/permissions"))
      return Promise.resolve(jsonResponse({}));
    if (url.startsWith("/audit"))
      return Promise.resolve(jsonResponse({ records }));
    return Promise.resolve(tokenResponse());
  });
}

// Pull the global audit-endpoint calls (the path carries the query string).
function auditCalls(): string[] {
  return fetchMock.mock.calls
    .map((c) => (typeof c[0] === "string" ? c[0] : String(c[0])))
    .filter((p) => p.startsWith("/audit"));
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  clearAccessToken();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AdminAuditPage gating", () => {
  it("denies a non-admin and issues no audit request", async () => {
    signedInAs(MEMBER, []);

    renderApp({ path: "/admin/audit" });

    expect(
      await screen.findByText(t("admin.denied.title")),
    ).toBeInTheDocument();
    expect(auditCalls().length).toBe(0);
  });
});

describe("AdminAuditPage", () => {
  it("renders a row with its community and the empty/loaded states", async () => {
    signedInAs(ADMIN, [record()]);

    renderApp({ path: "/admin/audit" });

    expect(await screen.findByText("server:start")).toBeInTheDocument();
    // The global view shows the community column.
    expect(screen.getAllByText(COMMUNITY.id).length).toBeGreaterThan(0);
  });

  it("requests the first page with limit and offset only", async () => {
    signedInAs(ADMIN, [record()]);

    renderApp({ path: "/admin/audit" });

    await waitFor(() => expect(auditCalls().length).toBeGreaterThan(0));
    const url = new URL(auditCalls()[0], "http://x");
    expect(url.pathname).toBe("/audit");
    expect(url.searchParams.get("limit")).toBe("50");
    expect(url.searchParams.get("offset")).toBe("0");
    expect(url.searchParams.get("community")).toBeNull();
    expect(url.searchParams.get("operation")).toBeNull();
  });

  it("maps the filters incl. community to query params", async () => {
    signedInAs(ADMIN, [record()]);

    renderApp({ path: "/admin/audit" });
    await screen.findByText("server:start");

    fireEvent.change(screen.getByLabelText(t("admin.audit.filterCommunity")), {
      target: { value: COMMUNITY.id },
    });
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.audit.filterOperation")),
      { target: { value: "member:add" } },
    );
    const ACTOR = "22222222-2222-2222-2222-222222222222";
    const SINCE_LOCAL = "2026-01-01T00:00";
    const UNTIL_LOCAL = "2026-02-01T00:00";
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.audit.filterActor")),
      { target: { value: ACTOR } },
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.audit.filterSince")),
      { target: { value: SINCE_LOCAL } },
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.audit.filterUntil")),
      { target: { value: UNTIL_LOCAL } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.audit.apply"),
      }),
    );

    await waitFor(() => {
      const url = new URL(auditCalls().at(-1) as string, "http://x");
      expect(url.searchParams.get("community")).toBe(COMMUNITY.id);
      expect(url.searchParams.get("operation")).toBe("member:add");
      expect(url.searchParams.get("actor")).toBe(ACTOR);
      // since/until are sent as UTC instants; derive the expectation via the
      // same local→UTC conversion so the test is timezone-independent.
      expect(url.searchParams.get("since")).toBe(
        new Date(SINCE_LOCAL).toISOString(),
      );
      expect(url.searchParams.get("until")).toBe(
        new Date(UNTIL_LOCAL).toISOString(),
      );
      expect(url.searchParams.get("offset")).toBe("0");
    });
  });

  it("rejects a non-UUID actor inline without issuing a request", async () => {
    signedInAs(ADMIN, [record()]);

    renderApp({ path: "/admin/audit" });
    await screen.findByText("server:start");
    const before = auditCalls().length;

    fireEvent.change(
      screen.getByLabelText(t("communitySettings.audit.filterActor")),
      { target: { value: "not-a-uuid" } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.audit.apply"),
      }),
    );

    expect(
      await screen.findByText(t("communitySettings.audit.filterActorInvalid")),
    ).toBeInTheDocument();
    expect(auditCalls().length).toBe(before);
  });

  it("pages forward with an increased offset", async () => {
    // A full page (50) signals there may be a next page.
    signedInAs(
      ADMIN,
      Array.from({ length: 50 }, (_, i) => record({ id: `a${i}` })),
    );

    renderApp({ path: "/admin/audit" });
    await screen.findAllByText("server:start");

    await waitFor(() => {
      const first = new URL(auditCalls()[0], "http://x");
      expect(first.searchParams.get("offset")).toBe("0");
    });

    fireEvent.click(
      screen.getByRole("button", { name: t("communitySettings.audit.next") }),
    );
    await waitFor(() => {
      const url = new URL(auditCalls().at(-1) as string, "http://x");
      expect(url.searchParams.get("offset")).toBe("50");
    });
  });
});
