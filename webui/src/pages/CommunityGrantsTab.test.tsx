import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { setAccessToken } from "../auth/tokenStore.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { CommunitySettingsPage } from "./CommunitySettingsPage.tsx";

const CID = "c1";

const mockApi = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

vi.mock("../api/client.ts", async () => {
  const actual =
    await vi.importActual<typeof import("../api/client.ts")>(
      "../api/client.ts",
    );
  return { ...actual, api: mockApi };
});

let mockCan: Can = () => true;
const setCommunityId = vi.fn();
vi.mock("../permissions/ActiveCommunityProvider.tsx", () => ({
  useActiveCommunity: () => ({
    communityId: CID,
    setCommunityId,
    communities: [{ id: CID, name: "Sakura" }],
  }),
}));
vi.mock("../permissions/useCan.ts", () => ({ useCan: () => mockCan }));

function community() {
  return { id: CID, name: "Sakura" };
}

function member(over: Record<string, unknown> = {}) {
  return {
    membership_id: "m1",
    user_id: "u1",
    username: "alice",
    role_names: [],
    ...over,
  };
}

function server(over: Record<string, unknown> = {}) {
  return { id: "s1", name: "survival", ...over };
}

function grant(over: Record<string, unknown> = {}) {
  return {
    id: "g1",
    user_id: "u1",
    resource_type: "server",
    resource_id: "s1",
    permissions: ["server:start"],
    ...over,
  };
}

// Route `api.get` by path. The grants list endpoint may carry a `?user_id=`
// filter, so match on the path prefix and surface the query string to the test.
let lastGrantsPath = "";
function routeGet(opts: {
  members?: unknown[];
  servers?: unknown[];
  grants?: unknown[];
}) {
  lastGrantsPath = "";
  mockApi.get.mockImplementation((path: string) => {
    if (path === `/api/communities/${CID}/members`) {
      return Promise.resolve(opts.members ?? []);
    }
    if (path === `/api/communities/${CID}/servers`) {
      return Promise.resolve(opts.servers ?? []);
    }
    if (path.startsWith(`/api/communities/${CID}/grants`)) {
      lastGrantsPath = path;
      return Promise.resolve(opts.grants ?? []);
    }
    return Promise.resolve(community());
  });
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={[`/communities/${CID}/settings`]}>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <Routes>
            <Route
              path="/communities/:cid/settings"
              element={<CommunitySettingsPage />}
            />
          </Routes>
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

// The page lands on Members; switch to the Grants tab.
async function openGrants() {
  await screen.findAllByText("Sakura");
  fireEvent.click(
    screen.getByRole("tab", { name: t("communitySettings.tab.grants") }),
  );
}

describe("CommunityGrantsTab", () => {
  beforeEach(() => {
    setAccessToken("tok-1");
    mockApi.get.mockReset();
    mockApi.post.mockReset();
    mockApi.patch.mockReset();
    mockApi.put.mockReset();
    mockApi.delete.mockReset();
    mockCan = () => true;
    setCommunityId.mockReset();
  });
  afterEach(() => vi.clearAllMocks());

  it("lists grants with member, server, and permission codes", async () => {
    routeGet({
      members: [member({ username: "alice" })],
      servers: [server({ id: "s1", name: "survival" })],
      grants: [grant({ permissions: ["server:start", "file:read"] })],
    });
    renderPage();
    await openGrants();

    // "alice" appears both in the member filter and the grant row.
    expect((await screen.findAllByText("alice")).length).toBeGreaterThan(0);
    expect(screen.getByText("survival")).toBeInTheDocument();
    expect(screen.getByText("server:start")).toBeInTheDocument();
    expect(screen.getByText("file:read")).toBeInTheDocument();
  });

  it("shows the empty state when there are no grants", async () => {
    routeGet({ members: [member()], servers: [server()], grants: [] });
    renderPage();
    await openGrants();

    expect(
      await screen.findByText(t("communitySettings.grants.empty")),
    ).toBeInTheDocument();
  });

  it("filters the grants list by member with a ?user_id= request", async () => {
    routeGet({
      members: [
        member({ user_id: "u1", username: "alice" }),
        member({ membership_id: "m2", user_id: "u2", username: "bob" }),
      ],
      servers: [server()],
      grants: [grant()],
    });
    renderPage();
    await openGrants();

    await screen.findAllByText("alice");
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.grants.filterLabel")),
      { target: { value: "u2" } },
    );

    await waitFor(() => {
      expect(lastGrantsPath).toBe(`/api/communities/${CID}/grants?user_id=u2`);
    });
  });

  it("creates a grant with a POST carrying the chosen member, server, and codes", async () => {
    routeGet({
      members: [member({ user_id: "u1", username: "alice" })],
      servers: [server({ id: "s1", name: "survival" })],
      grants: [],
    });
    mockApi.post.mockResolvedValue(grant());
    renderPage();
    await openGrants();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.grants.create"),
      }),
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.grants.memberLabel")),
      { target: { value: "u1" } },
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.grants.serverLabel")),
      { target: { value: "s1" } },
    );
    fireEvent.click(screen.getByLabelText("server:start"));
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.grants.createSubmit"),
      }),
    );

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/grants`,
        {
          body: JSON.stringify({
            user_id: "u1",
            resource_type: "server",
            resource_id: "s1",
            permissions: ["server:start"],
          }),
        },
      );
    });
  });

  it("only offers server/file/backup permission codes in the create picker", async () => {
    routeGet({
      members: [member()],
      servers: [server()],
      grants: [],
    });
    renderPage();
    await openGrants();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.grants.create"),
      }),
    );

    // Grantable families are offered…
    expect(screen.getByLabelText("server:start")).toBeInTheDocument();
    expect(screen.getByLabelText("file:read")).toBeInTheDocument();
    expect(screen.getByLabelText("backup:create")).toBeInTheDocument();
    // …a non-grantable family code is not.
    expect(screen.queryByLabelText("member:add")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("role:manage")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("grant:manage")).not.toBeInTheDocument();
  });

  it("revokes a grant after the typed confirm, with a DELETE", async () => {
    routeGet({
      members: [member()],
      servers: [server()],
      grants: [grant({ id: "g1" })],
    });
    mockApi.delete.mockResolvedValue(undefined);
    renderPage();
    await openGrants();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.grants.revoke"),
      }),
    );
    fireEvent.change(
      screen.getByPlaceholderText(
        t("communitySettings.grants.revokeConfirmPhrase"),
      ),
      { target: { value: t("communitySettings.grants.revokeConfirmPhrase") } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.grants.revokeConfirm"),
      }),
    );

    await waitFor(() => {
      expect(mockApi.delete).toHaveBeenCalledWith(
        `/api/communities/${CID}/grants/g1`,
      );
    });
  });

  it("hides create/revoke controls without grant:manage", async () => {
    mockCan = (code: string) => code !== "grant:manage";
    routeGet({
      members: [member()],
      servers: [server()],
      grants: [grant()],
    });
    renderPage();
    await openGrants();

    await screen.findAllByText("alice");
    expect(
      screen.queryByRole("button", {
        name: t("communitySettings.grants.create"),
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: t("communitySettings.grants.revoke"),
      }),
    ).not.toBeInTheDocument();
  });

  it("shows the denied notice when grant:read is absent", async () => {
    mockCan = (code: string) => code !== "grant:read";
    routeGet({ members: [], servers: [], grants: [] });
    renderPage();
    await openGrants();

    expect(
      await screen.findByText(t("permissions.denied")),
    ).toBeInTheDocument();
  });

  it("degrades a 403 on the secondary member/server reads to raw-id rows without failing the tab", async () => {
    // Caller holds grant:read but not member:read / server:read: the secondary
    // label reads 403 and must degrade to raw ids, not collapse the tab (#471).
    mockApi.get.mockImplementation((path: string) => {
      if (path === `/api/communities/${CID}/members`) {
        return Promise.reject(
          new ApiError(403, { reason: "forbidden", permission: "member:read" }),
        );
      }
      if (path === `/api/communities/${CID}/servers`) {
        return Promise.reject(
          new ApiError(403, { reason: "forbidden", permission: "server:read" }),
        );
      }
      if (path.startsWith(`/api/communities/${CID}/grants`)) {
        return Promise.resolve([grant({ user_id: "u9", resource_id: "s9" })]);
      }
      return Promise.resolve(community());
    });
    renderPage();
    await openGrants();

    // Row falls back to the raw user_id and resource_id…
    expect(await screen.findByText("u9")).toBeInTheDocument();
    expect(screen.getByText("s9")).toBeInTheDocument();
    // …and the tab is not collapsed into its generic load error.
    expect(
      screen.queryByText(t("communitySettings.grants.loadError")),
    ).not.toBeInTheDocument();
  });

  it("still fails the tab when the primary grants read 403s", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path === `/api/communities/${CID}/members`) {
        return Promise.resolve([member()]);
      }
      if (path === `/api/communities/${CID}/servers`) {
        return Promise.resolve([server()]);
      }
      if (path.startsWith(`/api/communities/${CID}/grants`)) {
        return Promise.reject(
          new ApiError(403, { reason: "forbidden", permission: "grant:read" }),
        );
      }
      return Promise.resolve(community());
    });
    renderPage();
    await openGrants();

    expect(
      await screen.findByText(t("communitySettings.grants.loadError")),
    ).toBeInTheDocument();
  });

  it("does not swallow a non-403 error on a secondary read", async () => {
    // A 500 on a secondary read is a real outage, not an authorization gap: it
    // must still surface the tab error rather than degrade silently.
    mockApi.get.mockImplementation((path: string) => {
      if (path === `/api/communities/${CID}/members`) {
        return Promise.reject(new ApiError(500, { reason: "server_error" }));
      }
      if (path === `/api/communities/${CID}/servers`) {
        return Promise.resolve([server()]);
      }
      if (path.startsWith(`/api/communities/${CID}/grants`)) {
        return Promise.resolve([grant()]);
      }
      return Promise.resolve(community());
    });
    renderPage();
    await openGrants();

    expect(
      await screen.findByText(t("communitySettings.grants.loadError")),
    ).toBeInTheDocument();
  });

  it("shows resolved labels when the caller holds the secondary gates", async () => {
    routeGet({
      members: [member({ user_id: "u1", username: "alice" })],
      servers: [server({ id: "s1", name: "survival" })],
      grants: [grant({ user_id: "u1", resource_id: "s1" })],
    });
    renderPage();
    await openGrants();

    expect((await screen.findAllByText("alice")).length).toBeGreaterThan(0);
    expect(screen.getByText("survival")).toBeInTheDocument();
    // The resolved names are shown, not the raw ids.
    expect(screen.queryByText("u1")).not.toBeInTheDocument();
    expect(screen.queryByText("s1")).not.toBeInTheDocument();
  });

  it("403 on servers label in grants tab does not poison the shared servers cache (#791)", async () => {
    // The grants tab uses a distinct query key ("grants-labels" suffix) so a
    // 403→[] fallback is isolated and does not write [] into the shared
    // ["communities", cid, "servers"] key that the Groups/Dashboard use.
    // Simulate: grants tab gets a 403 on servers; groups tab (which uses the
    // shared key) must still see real server data, not [].
    let groupsTabServersCallCount = 0;
    mockApi.get.mockImplementation((path: string) => {
      if (path === `/api/communities/${CID}/members`) {
        return Promise.resolve([member()]);
      }
      if (path === `/api/communities/${CID}/servers`) {
        groupsTabServersCallCount++;
        if (groupsTabServersCallCount === 1) {
          // First call comes from grants tab; 403 to force the fallback.
          return Promise.reject(
            new ApiError(403, {
              reason: "forbidden",
              permission: "server:read",
            }),
          );
        }
        // Subsequent calls (e.g., from groups tab) return real data.
        return Promise.resolve([server({ id: "s1", name: "survival" })]);
      }
      if (path.startsWith(`/api/communities/${CID}/grants`)) {
        return Promise.resolve([grant()]);
      }
      if (path.startsWith(`/api/communities/${CID}/groups`)) {
        return Promise.resolve([]);
      }
      return Promise.resolve(community());
    });
    renderPage();
    await openGrants();

    // Grants tab is visible with degraded raw ids (403 on servers).
    await screen.findAllByText(t("communitySettings.grants.colMember"));

    // The shared cache key must NOT have been poisoned with []. Switching to
    // the Groups tab should trigger a fresh fetch (distinct key → no stale hit)
    // and successfully load the server list.
    fireEvent.click(
      screen.getByRole("tab", { name: t("communitySettings.tab.groups") }),
    );

    // Groups tab loads without showing a "no servers" error caused by stale [].
    expect(
      await screen.findByText(t("communitySettings.groups.heading")),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(t("communitySettings.grants.loadError")),
    ).not.toBeInTheDocument();
  });

  it("routes a 403 on revoke through onForbidden (named-permission toast)", async () => {
    routeGet({
      members: [member()],
      servers: [server()],
      grants: [grant({ id: "g1" })],
    });
    mockApi.delete.mockRejectedValue(
      new ApiError(403, { reason: "forbidden", permission: "grant:manage" }),
    );
    renderPage();
    await openGrants();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.grants.revoke"),
      }),
    );
    fireEvent.change(
      screen.getByPlaceholderText(
        t("communitySettings.grants.revokeConfirmPhrase"),
      ),
      { target: { value: t("communitySettings.grants.revokeConfirmPhrase") } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.grants.revokeConfirm"),
      }),
    );

    expect(
      await screen.findByText(`${t("permissions.deniedNamed")}grant:manage`),
    ).toBeInTheDocument();
  });
});
