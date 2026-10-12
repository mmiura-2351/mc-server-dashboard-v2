import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "../auth/tokenStore.ts";
import { t } from "../i18n/index.ts";
import { renderApp } from "../test/render.tsx";

const COMMUNITY = { id: "11111111-1111-1111-1111-111111111111", name: "Alpha" };
// Include a community outside the admin's memberships to distinguish the admin-wide listing.
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

    expect(
      await screen.findByText(t("communitySettings.audit.op.server:start")),
    ).toBeInTheDocument();
    const communityCells = screen.getAllByText(COMMUNITY.id);
    expect(communityCells.length).toBeGreaterThan(0);
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

    const actorCell = (await screen.findByText("alice")).closest("td");
    expect(actorCell).toHaveAttribute("title", "u9");
    const targetCell = screen
      .getByText(`${t("communitySettings.audit.targetType.server")}: survival`)
      .closest("td");
    expect(targetCell).toHaveAttribute("title", "server:s9");
    // Alpha also appears in the picker; scope the name assertion to its table cell.
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

    expect((await screen.findByText("u9")).closest("td")).toHaveAttribute(
      "title",
      "u9",
    );
    expect(
      screen.getByText(`${t("communitySettings.audit.targetType.server")}: s9`),
    ).toBeInTheDocument();
    const communityCells = screen.getAllByText(COMMUNITY.id);
    expect(communityCells.length).toBeGreaterThan(0);
  });

  it("warns that the community picker is truncated when more communities exist than one page", async () => {
    // Report a total above the picker's page size to exercise its truncation hint.
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

    // Match the sentence's final static segment without depending on interpolated counts.
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
    // Bravo is absent from the membership list, so its option proves the picker reads the admin-wide endpoint.
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
      // Convert expected local datetimes to UTC so the assertion is timezone-independent.
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

    await waitFor(() => {
      const url = new URL(auditCalls().at(-1) as string, "http://x");
      expect(url.searchParams.get("community")).toBe(COMMUNITY.id);
      expect(url.searchParams.get("operation")).toBe("member:add");
      expect(url.searchParams.get("actor")).toBe(ACTOR);
    });
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

  it("pages forward with an increased offset", async () => {
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
