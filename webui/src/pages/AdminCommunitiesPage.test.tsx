import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "../auth/tokenStore.ts";
import { t } from "../i18n/index.ts";
import { renderApp } from "../test/render.tsx";

// Admin Communities page tests (#476). The app is driven through the real router
// and providers via renderApp; a fetch mock dispatches on URL + method so a
// single test can stand up /users/me, the community list (which doubles as the
// switcher's ["communities"] query), the user picker (GET /users) and the
// provision POST.

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

const COMMUNITIES = [
  { id: "c1", name: "Sakura SMP" },
  { id: "c2", name: "Dev Playground" },
];

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

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  clearAccessToken();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("admin communities list", () => {
  it("lists every community the admin sees, with its id", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/users/me") return Promise.resolve(jsonResponse(ADMIN));
      if (url === "/communities")
        return Promise.resolve(jsonResponse(COMMUNITIES));
      if (url.endsWith("/me/permissions"))
        return Promise.resolve(jsonResponse({}));
      if (url.startsWith("/users")) return Promise.resolve(jsonResponse(USERS));
      return Promise.resolve(tokenResponse());
    });

    renderApp({ path: "/admin/communities" });

    expect(
      await screen.findByRole("heading", { name: t("page.adminCommunities") }),
    ).toBeInTheDocument();
    // Scope to the page's data table: the community switcher in the shell also
    // renders these names (as <option>s), so query within the table.
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Sakura SMP")).toBeInTheDocument();
    expect(within(table).getByText("Dev Playground")).toBeInTheDocument();
    expect(within(table).getByText("c1")).toBeInTheDocument();
    expect(within(table).getByText("c2")).toBeInTheDocument();
  });
});

describe("admin communities owner picker", () => {
  it("requests the user list with the API max page size", async () => {
    let usersUrl: string | undefined;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/users/me") return Promise.resolve(jsonResponse(ADMIN));
      if (url === "/communities")
        return Promise.resolve(jsonResponse(COMMUNITIES));
      if (url.endsWith("/me/permissions"))
        return Promise.resolve(jsonResponse({}));
      if (url.startsWith("/users")) {
        usersUrl = url;
        return Promise.resolve(jsonResponse(USERS));
      }
      return Promise.resolve(tokenResponse());
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
      if (url === "/users/me") return Promise.resolve(jsonResponse(ADMIN));
      if (url === "/communities")
        return Promise.resolve(jsonResponse(COMMUNITIES));
      if (url.endsWith("/me/permissions"))
        return Promise.resolve(jsonResponse({}));
      if (url.startsWith("/users"))
        // 150 users in total but the page only returns the two stubbed here.
        return Promise.resolve(jsonResponse({ ...USERS, total: 150 }));
      return Promise.resolve(tokenResponse());
    });

    renderApp({ path: "/admin/communities" });

    await screen.findByRole("table");
    fireEvent.click(
      screen.getByRole("button", { name: t("admin.communities.provision") }),
    );
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("option", { name: /alice/ });

    // The hint names how many of the total are shown so the admin knows the
    // picker is incomplete (2 loaded of 150). The counts and the surrounding
    // copy are interleaved text nodes, so assert on the whole composed string.
    const prefix = within(dialog).getByText(
      t("admin.communities.usersTruncatedPrefix"),
      { exact: false },
    );
    expect(prefix.textContent).toContain("2");
    expect(prefix.textContent).toContain("150");
  });

  it("does not warn when the whole user list fits in one page", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/users/me") return Promise.resolve(jsonResponse(ADMIN));
      if (url === "/communities")
        return Promise.resolve(jsonResponse(COMMUNITIES));
      if (url.endsWith("/me/permissions"))
        return Promise.resolve(jsonResponse({}));
      if (url.startsWith("/users")) return Promise.resolve(jsonResponse(USERS));
      return Promise.resolve(tokenResponse());
    });

    renderApp({ path: "/admin/communities" });

    await screen.findByRole("table");
    fireEvent.click(
      screen.getByRole("button", { name: t("admin.communities.provision") }),
    );
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("option", { name: /alice/ });

    expect(
      within(dialog).queryByText(t("admin.communities.usersTruncatedPrefix"), {
        exact: false,
      }),
    ).not.toBeInTheDocument();
  });
});

describe("admin communities provisioning", () => {
  it("sends name + owner_user_id and invalidates the communities list", async () => {
    let communitiesCalls = 0;
    let provisionBody: unknown;
    fetchMock.mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        const m = method(input, init);
        if (url === "/users/me") return Promise.resolve(jsonResponse(ADMIN));
        if (url === "/communities" && m === "GET") {
          communitiesCalls += 1;
          return Promise.resolve(jsonResponse(COMMUNITIES));
        }
        if (url === "/communities" && m === "POST") {
          provisionBody = JSON.parse(init?.body as string);
          return Promise.resolve(
            jsonResponse({ id: "c3", name: "Winter 2026" }, 201),
          );
        }
        if (url.endsWith("/me/permissions"))
          return Promise.resolve(jsonResponse({}));
        if (url.startsWith("/users"))
          return Promise.resolve(jsonResponse(USERS));
        return Promise.resolve(tokenResponse());
      },
    );

    renderApp({ path: "/admin/communities" });

    await screen.findByRole("table");
    const callsBefore = communitiesCalls;

    fireEvent.click(
      screen.getByRole("button", { name: t("admin.communities.provision") }),
    );
    const dialog = await screen.findByRole("dialog");
    // The owner select is populated from GET /users; wait for the option.
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
    // The list query (shared with the switcher's ["communities"] key) is
    // invalidated, so it refetches after a successful provision.
    await waitFor(() => {
      expect(communitiesCalls).toBeGreaterThan(callsBefore);
    });
  });

  it("surfaces a name conflict inline without closing the dialog", async () => {
    fetchMock.mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        const m = method(input, init);
        if (url === "/users/me") return Promise.resolve(jsonResponse(ADMIN));
        if (url === "/communities" && m === "GET")
          return Promise.resolve(jsonResponse(COMMUNITIES));
        if (url === "/communities" && m === "POST")
          return Promise.resolve(problemResponse("name_taken", 409));
        if (url.endsWith("/me/permissions"))
          return Promise.resolve(jsonResponse({}));
        if (url.startsWith("/users"))
          return Promise.resolve(jsonResponse(USERS));
        return Promise.resolve(tokenResponse());
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
    // Dialog stays open so the admin can correct the name.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
