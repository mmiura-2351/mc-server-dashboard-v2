import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "../auth/tokenStore.ts";
import { t } from "../i18n/index.ts";
import { renderApp } from "../test/render.tsx";

// Global admin Audit page (#479). Driven through the real router and providers
// via renderApp; a fetch mock dispatches on URL so a single test can stand up
// /users/me, the community list, and the platform-admin global `/api/audit`
// endpoint (path carries the query string).

const COMMUNITY = { id: "11111111-1111-1111-1111-111111111111", name: "Alpha" };
// A second community the admin is NOT a member of: it must still appear in the
// picker, proving it is sourced from the admin-wide endpoint (#489).
const OTHER_COMMUNITY = {
  id: "33333333-3333-3333-3333-333333333333",
  name: "Bravo",
};

function adminCommunity(over: Record<string, unknown> = {}) {
  return {
    id: COMMUNITY.id,
    name: COMMUNITY.name,
    created_at: "2026-06-06T12:00:00Z",
    member_count: 1,
    server_count: 0,
    ...over,
  };
}

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
function signedInAs(
  user: typeof ADMIN,
  records: unknown[],
  adminCommunities: unknown[] = [adminCommunity()],
) {
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url === "/api/users/me") return Promise.resolve(jsonResponse(user));
    if (url === "/api/communities")
      return Promise.resolve(jsonResponse([COMMUNITY]));
    if (url.startsWith("/api/admin/communities"))
      return Promise.resolve(
        jsonResponse({
          total: adminCommunities.length,
          limit: 100,
          offset: 0,
          communities: adminCommunities,
        }),
      );
    if (url.endsWith("/me/permissions"))
      return Promise.resolve(jsonResponse({}));
    if (url.startsWith("/api/audit"))
      return Promise.resolve(jsonResponse({ records }));
    return Promise.resolve(tokenResponse());
  });
}

// Pull the global audit-endpoint calls (the path carries the query string).
function auditCalls(): string[] {
  return fetchMock.mock.calls
    .map((c) => (typeof c[0] === "string" ? c[0] : String(c[0])))
    .filter((p) => p.startsWith("/api/audit"));
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

    // Operation code humanized to its readable label (#643).
    expect(
      await screen.findByText(t("communitySettings.audit.op.server:start")),
    ).toBeInTheDocument();
    // The global view shows the community column.
    const communityCells = screen.getAllByText(COMMUNITY.id);
    expect(communityCells.length).toBeGreaterThan(0);
    // The community cell carries the full id as a hover title (#519).
    expect(communityCells[0].closest("td")).toHaveAttribute(
      "title",
      COMMUNITY.id,
    );
  });

  it("renders resolved actor/target/community names, keeping raw ids on hover (#682)", async () => {
    signedInAs(ADMIN, [
      record({
        actor_id: "u9",
        actor_username: "alice",
        community_id: COMMUNITY.id,
        community_name: "Alpha",
        target_type: "server",
        target_id: "s9",
        target_name: "survival",
      }),
    ]);

    renderApp({ path: "/admin/audit" });

    // Actor shows the resolved username; the raw id stays in the hover title.
    const actorCell = (await screen.findByText("alice")).closest("td");
    expect(actorCell).toHaveAttribute("title", "u9");
    // Target shows the humanized type prefix + the resolved server name; the raw
    // "type:id" stays in the title.
    const targetCell = screen
      .getByText(`${t("communitySettings.audit.targetType.server")}: survival`)
      .closest("td");
    expect(targetCell).toHaveAttribute("title", "server:s9");
    // Community column shows the resolved name; the raw id stays in the title.
    // "Alpha" also appears as a picker option, so scope to the table cell.
    const communityCell = screen
      .getAllByText("Alpha")
      .map((el) => el.closest("td"))
      .find((cell) => cell !== null);
    expect(communityCell).toHaveAttribute("title", COMMUNITY.id);
  });

  it("falls back to raw ids when names are absent (deleted subject, #682)", async () => {
    signedInAs(ADMIN, [
      record({
        actor_id: "u9",
        actor_username: null,
        community_id: COMMUNITY.id,
        community_name: null,
        target_type: "server",
        target_id: "s9",
        target_name: null,
      }),
    ]);

    renderApp({ path: "/admin/audit" });

    // Actor falls back to the raw id.
    expect((await screen.findByText("u9")).closest("td")).toHaveAttribute(
      "title",
      "u9",
    );
    // Target falls back to the humanized type + raw id.
    expect(
      screen.getByText(`${t("communitySettings.audit.targetType.server")}: s9`),
    ).toBeInTheDocument();
    // Community falls back to the raw id.
    const communityCells = screen.getAllByText(COMMUNITY.id);
    expect(communityCells.length).toBeGreaterThan(0);
  });

  it("warns that the community picker is truncated when more communities exist than one page", async () => {
    // The picker requests one page (limit=100); report a larger total so the
    // hint surfaces, mirroring the Provision owner picker (#476/#488).
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/users/me") return Promise.resolve(jsonResponse(ADMIN));
      if (url === "/api/communities")
        return Promise.resolve(jsonResponse([COMMUNITY]));
      if (url.startsWith("/api/admin/communities"))
        return Promise.resolve(
          jsonResponse({
            total: 150,
            limit: 100,
            offset: 0,
            communities: [adminCommunity()],
          }),
        );
      if (url.endsWith("/me/permissions"))
        return Promise.resolve(jsonResponse({}));
      if (url.startsWith("/api/audit"))
        return Promise.resolve(jsonResponse({ records: [record()] }));
      return Promise.resolve(tokenResponse());
    });

    renderApp({ path: "/admin/audit" });
    await screen.findByText(t("communitySettings.audit.op.server:start"));

    expect(
      await screen.findByText(
        t("admin.audit.communitiesTruncated", { n: 1, total: 150 }),
      ),
    ).toBeInTheDocument();
  });

  it("does not warn when the whole community list fits in one page", async () => {
    signedInAs(ADMIN, [record()], [adminCommunity()]);

    renderApp({ path: "/admin/audit" });
    await screen.findByText(t("communitySettings.audit.op.server:start"));

    // No truncation hint at all: match the static, value-free tail of the
    // single interpolated sentence (last segment after the final placeholder).
    const truncatedTail = t("admin.audit.communitiesTruncated")
      .split("}")
      .pop();
    if (truncatedTail === undefined) throw new Error("no tail");
    expect(
      screen.queryByText(truncatedTail, { exact: false }),
    ).not.toBeInTheDocument();
  });

  it("sources the community picker from the admin-wide endpoint (incl. non-member)", async () => {
    signedInAs(
      ADMIN,
      [record()],
      [
        adminCommunity(),
        adminCommunity({ id: OTHER_COMMUNITY.id, name: OTHER_COMMUNITY.name }),
      ],
    );

    renderApp({ path: "/admin/audit" });
    await screen.findByText(t("communitySettings.audit.op.server:start"));

    const picker = screen.getByLabelText(t("admin.audit.filterCommunity"));
    // "Bravo" is not in the membership-scoped switcher list, so its presence as
    // an option proves the picker reads GET /admin/communities.
    expect(
      within(picker).getByRole("option", { name: OTHER_COMMUNITY.name }),
    ).toBeInTheDocument();
  });

  it("requests the first page with limit and offset only", async () => {
    signedInAs(ADMIN, [record()]);

    renderApp({ path: "/admin/audit" });

    await waitFor(() => expect(auditCalls().length).toBeGreaterThan(0));
    const url = new URL(auditCalls()[0], "http://x");
    expect(url.pathname).toBe("/api/audit");
    expect(url.searchParams.get("limit")).toBe("50");
    expect(url.searchParams.get("offset")).toBe("0");
    expect(url.searchParams.get("community")).toBeNull();
    expect(url.searchParams.get("operation")).toBeNull();
  });

  it("maps the filters incl. community to query params", async () => {
    signedInAs(ADMIN, [record()]);

    renderApp({ path: "/admin/audit" });
    await screen.findByText(t("communitySettings.audit.op.server:start"));

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
    await screen.findByText(t("communitySettings.audit.op.server:start"));
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

  it("restores filters from the URL query string on load (deep link / reload)", async () => {
    signedInAs(ADMIN, [record()]);
    const ACTOR = "22222222-2222-2222-2222-222222222222";

    renderApp({
      path: `/admin/audit?community=${COMMUNITY.id}&operation=member%3Aadd&actor=${ACTOR}`,
    });
    await screen.findByText(t("communitySettings.audit.op.server:start"));

    // The persisted filters drive the first request without any Apply click.
    await waitFor(() => {
      const url = new URL(auditCalls().at(-1) as string, "http://x");
      expect(url.searchParams.get("community")).toBe(COMMUNITY.id);
      expect(url.searchParams.get("operation")).toBe("member:add");
      expect(url.searchParams.get("actor")).toBe(ACTOR);
    });
    // The inputs reflect the restored filters.
    expect(
      (
        screen.getByLabelText(
          t("communitySettings.audit.filterOperation"),
        ) as HTMLInputElement
      ).value,
    ).toBe("member:add");
    expect(
      (
        screen.getByLabelText(
          t("admin.audit.filterCommunity"),
        ) as HTMLSelectElement
      ).value,
    ).toBe(COMMUNITY.id);
  });

  it("keeps rendering cached audit records when a background refetch fails (#1805)", async () => {
    signedInAs(ADMIN, [record()]);
    const { queryClient } = renderApp({ path: "/admin/audit" });
    await screen.findByText(t("communitySettings.audit.op.server:start"));

    // Simulate a transient API outage: the audit endpoint fails.
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/users/me") return Promise.resolve(jsonResponse(ADMIN));
      if (url === "/api/communities")
        return Promise.resolve(jsonResponse([COMMUNITY]));
      if (url.startsWith("/api/admin/communities"))
        return Promise.resolve(
          jsonResponse({
            total: 1,
            limit: 100,
            offset: 0,
            communities: [adminCommunity()],
          }),
        );
      if (url.endsWith("/me/permissions"))
        return Promise.resolve(jsonResponse({}));
      if (url.startsWith("/api/audit"))
        return Promise.resolve(new Response("nope", { status: 503 }));
      return Promise.resolve(tokenResponse());
    });
    await act(() => queryClient.invalidateQueries());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // The cached records stay on screen instead of the error.
    expect(
      screen.getByText(t("communitySettings.audit.op.server:start")),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(t("communitySettings.audit.loadError")),
    ).not.toBeInTheDocument();
  });

  it("pages forward with an increased offset", async () => {
    // A full page (50) signals there may be a next page.
    signedInAs(
      ADMIN,
      Array.from({ length: 50 }, (_, i) => record({ id: `a${i}` })),
    );

    renderApp({ path: "/admin/audit" });
    await screen.findAllByText(t("communitySettings.audit.op.server:start"));

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
