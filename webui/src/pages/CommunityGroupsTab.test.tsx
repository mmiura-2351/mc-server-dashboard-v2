import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { attachmentsKeys } from "../api/communityQueryKeys.ts";
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

function group(over: Record<string, unknown> = {}) {
  return {
    id: "g1",
    community_id: CID,
    name: "Admins",
    kind: "op",
    players: [],
    ...over,
  };
}

function server(over: Record<string, unknown> = {}) {
  return { id: "s1", name: "Survival", ...over };
}

// Route `api.get` by path: the page reads the community, the Groups tab reads
// the group list, the community server list, and (on expand) a group's servers.
function routeGet(opts: {
  groups?: unknown[];
  servers?: unknown[];
  groupServers?: string[];
}) {
  mockApi.get.mockImplementation((path: string) => {
    if (path === `/api/communities/${CID}/groups`) {
      return Promise.resolve(opts.groups ?? []);
    }
    if (path === `/api/communities/${CID}/servers`) {
      return Promise.resolve(opts.servers ?? []);
    }
    if (/\/groups\/[^/]+\/servers$/.test(path)) {
      return Promise.resolve(opts.groupServers ?? []);
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

// The Groups tab is not the default; land on the page then switch to it.
async function openGroupsTab() {
  await screen.findAllByText("Sakura");
  fireEvent.click(
    screen.getByRole("tab", { name: t("communitySettings.tab.groups") }),
  );
}

describe("CommunityGroupsTab", () => {
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

  it("lists groups with kind badges", async () => {
    routeGet({
      groups: [
        group({ id: "g1", name: "Admins", kind: "op" }),
        group({ id: "g2", name: "Friends", kind: "whitelist" }),
      ],
    });
    renderPage();
    await openGroupsTab();

    expect(await screen.findByText("Admins")).toBeInTheDocument();
    expect(screen.getByText("Friends")).toBeInTheDocument();
    expect(
      screen.getByText(t("communitySettings.groups.kind.op")),
    ).toBeInTheDocument();
    expect(
      screen.getByText(t("communitySettings.groups.kind.whitelist")),
    ).toBeInTheDocument();
  });

  it("shows the empty state when there are no groups", async () => {
    routeGet({ groups: [] });
    renderPage();
    await openGroupsTab();

    expect(
      await screen.findByText(t("communitySettings.groups.empty")),
    ).toBeInTheDocument();
  });

  it("creates a group with a POST carrying {name, kind}", async () => {
    routeGet({ groups: [] });
    mockApi.post.mockResolvedValue(group());
    renderPage();
    await openGroupsTab();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.create"),
      }),
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.groups.nameLabel")),
      { target: { value: "Mods" } },
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.groups.kindLabel")),
      { target: { value: "whitelist" } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.groups.createSubmit"),
      }),
    );

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/groups`,
        {
          body: JSON.stringify({ name: "Mods", kind: "whitelist" }),
        },
      );
    });
  });

  it("renames a group with a PATCH carrying {name}", async () => {
    routeGet({ groups: [group({ name: "Admins" })] });
    mockApi.patch.mockResolvedValue(group({ name: "Owners" }));
    renderPage();
    await openGroupsTab();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.rename"),
      }),
    );
    const input = screen.getByLabelText(
      t("communitySettings.groups.nameLabel"),
    );
    fireEvent.change(input, { target: { value: "Owners" } });
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.groups.renameSubmit"),
      }),
    );

    await waitFor(() => {
      expect(mockApi.patch).toHaveBeenCalledWith(
        `/api/communities/${CID}/groups/g1`,
        { body: JSON.stringify({ name: "Owners" }) },
      );
    });
  });

  it("deletes a group after the typed confirm, with a DELETE", async () => {
    routeGet({ groups: [group({ name: "Admins" })] });
    mockApi.delete.mockResolvedValue(undefined);
    renderPage();
    await openGroupsTab();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.delete"),
      }),
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.groups.deleteConfirm"),
      }),
    );

    await waitFor(() => {
      expect(mockApi.delete).toHaveBeenCalledWith(
        `/api/communities/${CID}/groups/g1`,
      );
    });
  });

  it("adds a player with a POST carrying {uuid, username}", async () => {
    routeGet({ groups: [group({ players: [] })], groupServers: [] });
    mockApi.post.mockResolvedValue(group());
    renderPage();
    await openGroupsTab();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.expand"),
      }),
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.groups.uuidLabel")),
      { target: { value: "uuid-1" } },
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.groups.usernameLabel")),
      { target: { value: "steve" } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.groups.addPlayer"),
      }),
    );

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/groups/g1/players`,
        { body: JSON.stringify({ uuid: "uuid-1", username: "steve" }) },
      );
    });
  });

  it("removes a player with a DELETE after confirmation dialog", async () => {
    routeGet({
      groups: [group({ players: [{ uuid: "uuid-1", username: "steve" }] })],
      groupServers: [],
    });
    mockApi.delete.mockResolvedValue(group());
    renderPage();
    await openGroupsTab();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.expand"),
      }),
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.removePlayer"),
      }),
    );

    // The confirm dialog must appear before the DELETE fires.
    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.removePlayerConfirm"),
      }),
    );

    await waitFor(() => {
      expect(mockApi.delete).toHaveBeenCalledWith(
        `/api/communities/${CID}/groups/g1/players/uuid-1`,
      );
    });
  });

  // #611: the Players tab renders group.players.length from the attachments
  // (server's-groups) projection, so an add/remove must invalidate that prefix
  // too, not only the groups list, or the count stays stale until remount.
  it("invalidates the attachments prefix after adding a player", async () => {
    const invalidate = vi.spyOn(QueryClient.prototype, "invalidateQueries");
    routeGet({ groups: [group({ players: [] })], groupServers: [] });
    mockApi.post.mockResolvedValue(group());
    renderPage();
    await openGroupsTab();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.expand"),
      }),
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.groups.uuidLabel")),
      { target: { value: "uuid-1" } },
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.groups.usernameLabel")),
      { target: { value: "steve" } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.groups.addPlayer"),
      }),
    );

    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: attachmentsKeys.all(CID),
      });
    });
  });

  it("invalidates the attachments prefix after removing a player", async () => {
    const invalidate = vi.spyOn(QueryClient.prototype, "invalidateQueries");
    routeGet({
      groups: [group({ players: [{ uuid: "uuid-1", username: "steve" }] })],
      groupServers: [],
    });
    mockApi.delete.mockResolvedValue(group());
    renderPage();
    await openGroupsTab();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.expand"),
      }),
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.removePlayer"),
      }),
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.removePlayerConfirm"),
      }),
    );

    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: attachmentsKeys.all(CID),
      });
    });
  });

  it("attaches a server with a PUT, filtering already-attached servers from the picker", async () => {
    routeGet({
      groups: [group()],
      servers: [
        server({ id: "s1", name: "Survival" }),
        server({ id: "s2", name: "Creative" }),
      ],
      groupServers: ["s1"],
    });
    mockApi.put.mockResolvedValue(undefined);
    renderPage();
    await openGroupsTab();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.expand"),
      }),
    );

    // Survival is attached (it gets a Detach button once the per-group server
    // query resolves); Creative is then the only attach candidate.
    await screen.findByRole("button", {
      name: t("communitySettings.groups.detach"),
    });
    expect(screen.getByText("Creative")).toBeInTheDocument();
    expect(
      screen.getAllByRole("button", {
        name: t("communitySettings.groups.attach"),
      }),
    ).toHaveLength(1);

    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.groups.attach"),
      }),
    );

    await waitFor(() => {
      expect(mockApi.put).toHaveBeenCalledWith(
        `/api/communities/${CID}/groups/g1/servers/s2`,
      );
    });
  });

  it("detaches a server with a DELETE to the server route", async () => {
    routeGet({
      groups: [group()],
      servers: [server({ id: "s1", name: "Survival" })],
      groupServers: ["s1"],
    });
    mockApi.delete.mockResolvedValue(undefined);
    renderPage();
    await openGroupsTab();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.expand"),
      }),
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.detach"),
      }),
    );

    await waitFor(() => {
      expect(mockApi.delete).toHaveBeenCalledWith(
        `/api/communities/${CID}/groups/g1/servers/s1`,
      );
    });
  });

  it("hides create/rename/delete and player/server mutations without group:manage", async () => {
    mockCan = (code: string) => code !== "group:manage";
    routeGet({
      groups: [group({ players: [{ uuid: "uuid-1", username: "steve" }] })],
      groupServers: ["s1"],
      servers: [server()],
    });
    renderPage();
    await openGroupsTab();

    await screen.findByText("Admins");
    expect(
      screen.queryByRole("button", {
        name: t("communitySettings.groups.create"),
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: t("communitySettings.groups.rename"),
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: t("communitySettings.groups.delete"),
      }),
    ).not.toBeInTheDocument();

    // The read-only detail panel still opens, but offers no mutations.
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.groups.expand"),
      }),
    );
    expect(
      screen.queryByRole("button", {
        name: t("communitySettings.groups.removePlayer"),
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: t("communitySettings.groups.addPlayer"),
      }),
    ).not.toBeInTheDocument();
  });

  it("shows the denied notice when group:read is absent", async () => {
    mockCan = (code: string) => code !== "group:read";
    routeGet({ groups: [] });
    renderPage();
    await openGroupsTab();

    expect(
      await screen.findByText(t("permissions.denied")),
    ).toBeInTheDocument();
  });

  it("routes a 403 on create through onForbidden (named-permission toast)", async () => {
    routeGet({ groups: [] });
    mockApi.post.mockRejectedValue(
      new ApiError(403, { reason: "forbidden", permission: "group:manage" }),
    );
    renderPage();
    await openGroupsTab();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.groups.create"),
      }),
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.groups.nameLabel")),
      { target: { value: "Mods" } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.groups.createSubmit"),
      }),
    );

    expect(
      await screen.findByText(`${t("permissions.deniedNamed")}group:manage`),
    ).toBeInTheDocument();
  });
});
