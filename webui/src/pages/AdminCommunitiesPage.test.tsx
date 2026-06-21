import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "../auth/tokenStore.ts";
import { t } from "../i18n/index.ts";
import { renderApp } from "../test/render.tsx";

// Admin Communities page tests (#476, #489). The app is driven through the real
// router and providers via renderApp; a fetch mock dispatches on URL + method so
// a single test can stand up /users/me, the membership-scoped switcher list
// (GET /communities), the admin-wide page list (GET /admin/communities), the
// user picker (GET /admin/users), and the provision/delete calls.

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function problemResponse(reason: string, status: number): Response {
  return new Response(
    JSON.stringify({
      type: `urn:mcsd:error:${reason}`,
      title: reason,
      status,
      reason,
    }),
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

function tokenResponse(): Response {
  return jsonResponse({
    access_token: "fresh",
    refresh_token: "ignored",
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

// The membership-scoped switcher list (CommunityResponse: {id, name}).
const SWITCHER_COMMUNITIES = [{ id: "c1", name: "Sakura SMP" }];

// The admin-wide list (AdminCommunityResponse: id/name/created_at/counts).
const ADMIN_COMMUNITIES = {
  total: 2,
  limit: 50,
  offset: 0,
  communities: [
    {
      id: "c1",
      name: "Sakura SMP",
      created_at: new Date().toISOString(),
      member_count: 5,
      server_count: 3,
    },
    {
      id: "c2",
      name: "Dev Playground",
      created_at: new Date().toISOString(),
      member_count: 1,
      server_count: 0,
    },
  ],
};

const USERS = {
  total: 2,
  limit: 50,
  offset: 0,
  users: [
    {
      id: "u1",
      username: "admin",
      email: "admin@example.com",
      active: true,
      is_platform_admin: true,
      created_at: new Date().toISOString(),
    },
    {
      id: "u2",
      username: "alice",
      email: "alice@example.com",
      active: true,
      is_platform_admin: false,
      created_at: new Date().toISOString(),
    },
  ],
};

function method(input: RequestInfo | URL, init?: RequestInit): string {
  if (typeof input !== "string" && "method" in input) {
    return (input.method ?? "GET").toUpperCase();
  }
  return (init?.method ?? "GET").toUpperCase();
}

// Common dispatch for the supporting endpoints (me, switcher list, permissions,
// users). Page-specific routes are layered on by each test before this.
function baseRoute(url: string): Response | undefined {
  if (url === "/api/users/me") return jsonResponse(ADMIN);
  if (url === "/api/communities") return jsonResponse(SWITCHER_COMMUNITIES);
  if (url.endsWith("/me/permissions")) return jsonResponse({});
  if (url.startsWith("/api/admin/users")) return jsonResponse(USERS);
  return undefined;
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  clearAccessToken();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("admin communities list", () => {
  it("lists every community (incl. non-member) with its counts, from /admin/communities", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.startsWith("/api/admin/communities"))
        return Promise.resolve(jsonResponse(ADMIN_COMMUNITIES));
      return Promise.resolve(baseRoute(url) ?? tokenResponse());
    });

    renderApp({ path: "/admin/communities" });

    expect(
      await screen.findByRole("heading", { name: t("page.adminCommunities") }),
    ).toBeInTheDocument();
    const table = await screen.findByRole("table");
    // "Dev Playground" (c2) is NOT in the membership-scoped switcher list, so
    // its presence proves the page reads the admin-wide endpoint.
    expect(within(table).getByText("Dev Playground")).toBeInTheDocument();
    expect(within(table).getByText("c2")).toBeInTheDocument();
    // Counts render in the row.
    expect(within(table).getByText("5")).toBeInTheDocument();
    expect(within(table).getByText("3")).toBeInTheDocument();
    // The id cell carries the full id as a hover title (#519).
    expect(within(table).getByText("c2").closest("td")).toHaveAttribute(
      "title",
      "c2",
    );
  });
});

describe("admin communities delete", () => {
  it("deletes a community via DELETE /communities/{cid} after typed confirm and refetches", async () => {
    let deleted: string | undefined;
    let adminListCalls = 0;
    fetchMock.mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        const m = method(input, init);
        if (url.startsWith("/api/admin/communities")) {
          adminListCalls += 1;
          return Promise.resolve(jsonResponse(ADMIN_COMMUNITIES));
        }
        if (url.startsWith("/api/communities/") && m === "DELETE") {
          deleted = url;
          return Promise.resolve(new Response(null, { status: 204 }));
        }
        return Promise.resolve(baseRoute(url) ?? tokenResponse());
      },
    );

    renderApp({ path: "/admin/communities" });

    const table = await screen.findByRole("table");
    const callsBefore = adminListCalls;
    // Delete the second (non-member) community.
    const row = within(table).getByText("Dev Playground").closest("tr");
    if (row === null) throw new Error("row not found");
    fireEvent.click(
      within(row).getByRole("button", { name: t("admin.communities.delete") }),
    );

    const dialog = await screen.findByRole("dialog");
    // Typed-confirm: the destructive button stays disabled until the exact name.
    const confirm = within(dialog).getByRole("button", {
      name: t("admin.communities.deleteConfirm"),
    });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByRole("textbox"), {
      target: { value: "Dev Playground" },
    });
    expect(confirm).not.toBeDisabled();
    fireEvent.click(confirm);

    await waitFor(() => {
      expect(deleted).toBe("/api/communities/c2");
    });
    await waitFor(() => {
      expect(adminListCalls).toBeGreaterThan(callsBefore);
    });
  });

  it("treats a 404 (already gone) as success and refetches without an error toast", async () => {
    let adminListCalls = 0;
    fetchMock.mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        const m = method(input, init);
        if (url.startsWith("/api/admin/communities")) {
          adminListCalls += 1;
          return Promise.resolve(jsonResponse(ADMIN_COMMUNITIES));
        }
        if (url.startsWith("/api/communities/") && m === "DELETE") {
          return Promise.resolve(problemResponse("not_found", 404));
        }
        return Promise.resolve(baseRoute(url) ?? tokenResponse());
      },
    );

    renderApp({ path: "/admin/communities" });

    const table = await screen.findByRole("table");
    const callsBefore = adminListCalls;
    const row = within(table).getByText("Dev Playground").closest("tr");
    if (row === null) throw new Error("row not found");
    fireEvent.click(
      within(row).getByRole("button", { name: t("admin.communities.delete") }),
    );

    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByRole("textbox"), {
      target: { value: "Dev Playground" },
    });
    fireEvent.click(
      within(dialog).getByRole("button", {
        name: t("admin.communities.deleteConfirm"),
      }),
    );

    // 404 means the community is already gone: show the success message and
    // refetch the list, never the delete-error toast.
    expect(
      await screen.findByText(t("admin.communities.deleted")),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(adminListCalls).toBeGreaterThan(callsBefore);
    });
    expect(
      screen.queryByText(t("admin.communities.deleteError")),
    ).not.toBeInTheDocument();
  });
});

describe("admin communities owner picker", () => {
  it("requests the user list with the API max page size", async () => {
    let usersUrl: string | undefined;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.startsWith("/api/admin/communities"))
        return Promise.resolve(jsonResponse(ADMIN_COMMUNITIES));
      if (url.startsWith("/api/admin/users")) {
        usersUrl = url;
        return Promise.resolve(jsonResponse(USERS));
      }
      return Promise.resolve(baseRoute(url) ?? tokenResponse());
    });

    renderApp({ path: "/admin/communities" });

    await screen.findByRole("table");
    fireEvent.click(
      screen.getByRole("button", { name: t("admin.communities.provision") }),
    );
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("option", { name: /alice/ });

    const params = new URL(usersUrl ?? "", "http://localhost").searchParams;
    expect(params.get("limit")).toBe("100");
    expect(params.get("offset")).toBe("0");
  });

  it("warns that the picker is truncated when more users exist than one page", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.startsWith("/api/admin/communities"))
        return Promise.resolve(jsonResponse(ADMIN_COMMUNITIES));
      if (url.startsWith("/api/admin/users"))
        return Promise.resolve(jsonResponse({ ...USERS, total: 150 }));
      return Promise.resolve(baseRoute(url) ?? tokenResponse());
    });

    renderApp({ path: "/admin/communities" });

    await screen.findByRole("table");
    fireEvent.click(
      screen.getByRole("button", { name: t("admin.communities.provision") }),
    );
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("option", { name: /alice/ });

    expect(
      within(dialog).getByText(
        t("admin.communities.usersTruncated", { n: 2, total: 150 }),
      ),
    ).toBeInTheDocument();
  });

  it("does not warn when the whole user list fits in one page", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.startsWith("/api/admin/communities"))
        return Promise.resolve(jsonResponse(ADMIN_COMMUNITIES));
      return Promise.resolve(baseRoute(url) ?? tokenResponse());
    });

    renderApp({ path: "/admin/communities" });

    await screen.findByRole("table");
    fireEvent.click(
      screen.getByRole("button", { name: t("admin.communities.provision") }),
    );
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("option", { name: /alice/ });

    // No truncation hint at all: match the static, value-free tail of the
    // single interpolated sentence (last segment after the final placeholder).
    const truncatedTail = t("admin.communities.usersTruncated")
      .split("}")
      .pop();
    if (truncatedTail === undefined) throw new Error("no tail");
    expect(
      within(dialog).queryByText(truncatedTail, { exact: false }),
    ).not.toBeInTheDocument();
  });
});

describe("admin communities provisioning", () => {
  it("sends name + owner_user_id and invalidates the communities list", async () => {
    let adminListCalls = 0;
    let provisionBody: unknown;
    fetchMock.mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        const m = method(input, init);
        if (url.startsWith("/api/admin/communities")) {
          adminListCalls += 1;
          return Promise.resolve(jsonResponse(ADMIN_COMMUNITIES));
        }
        if (url === "/api/communities" && m === "POST") {
          provisionBody = JSON.parse(init?.body as string);
          return Promise.resolve(
            jsonResponse({ id: "c3", name: "Winter 2026" }, 201),
          );
        }
        return Promise.resolve(baseRoute(url) ?? tokenResponse());
      },
    );

    renderApp({ path: "/admin/communities" });

    await screen.findByRole("table");
    const callsBefore = adminListCalls;

    fireEvent.click(
      screen.getByRole("button", { name: t("admin.communities.provision") }),
    );
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("option", { name: /alice/ });
    fireEvent.change(
      within(dialog).getByLabelText(t("admin.communities.nameLabel")),
      { target: { value: "Winter 2026" } },
    );
    fireEvent.change(
      within(dialog).getByLabelText(t("admin.communities.ownerLabel")),
      { target: { value: "u2" } },
    );
    fireEvent.click(
      within(dialog).getByRole("button", {
        name: t("admin.communities.provisionSubmit"),
      }),
    );

    await waitFor(() => {
      expect(provisionBody).toEqual({
        name: "Winter 2026",
        owner_user_id: "u2",
      });
    });
    await waitFor(() => {
      expect(adminListCalls).toBeGreaterThan(callsBefore);
    });
  });

  it("surfaces a name conflict inline without closing the dialog", async () => {
    fetchMock.mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        const m = method(input, init);
        if (url.startsWith("/api/admin/communities"))
          return Promise.resolve(jsonResponse(ADMIN_COMMUNITIES));
        if (url === "/api/communities" && m === "POST")
          return Promise.resolve(problemResponse("name_taken", 409));
        return Promise.resolve(baseRoute(url) ?? tokenResponse());
      },
    );

    renderApp({ path: "/admin/communities" });

    await screen.findByRole("table");
    fireEvent.click(
      screen.getByRole("button", { name: t("admin.communities.provision") }),
    );
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("option", { name: /alice/ });
    fireEvent.change(
      within(dialog).getByLabelText(t("admin.communities.nameLabel")),
      { target: { value: "Sakura SMP" } },
    );
    fireEvent.change(
      within(dialog).getByLabelText(t("admin.communities.ownerLabel")),
      { target: { value: "u2" } },
    );
    fireEvent.click(
      within(dialog).getByRole("button", {
        name: t("admin.communities.provisionSubmit"),
      }),
    );

    expect(
      await within(dialog).findByText(t("admin.communities.errNameTaken")),
    ).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
