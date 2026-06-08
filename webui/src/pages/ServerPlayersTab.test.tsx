import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { setAccessToken } from "../auth/tokenStore.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { installMockWebSocket } from "../test/mockWebSocket.ts";
import { ServerDetailPage } from "./ServerDetailPage.tsx";

const CID = "c1";
const SID = "s1";

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
vi.mock("../permissions/ActiveCommunityProvider.tsx", () => ({
  useActiveCommunity: () => ({
    communityId: CID,
    setCommunityId: vi.fn(),
    communities: [{ id: CID, name: "Sakura" }],
  }),
}));
vi.mock("../permissions/useCan.ts", () => ({ useCan: () => mockCan }));

function serverResponse() {
  return {
    id: SID,
    community_id: CID,
    name: "survival",
    server_type: "paper",
    mc_edition: "java",
    mc_version: "1.21.6",
    execution_backend: "container",
    game_port: 25565,
    desired_state: "running",
    observed_state: "running",
    observed_at: null,
    assigned_worker_id: "worker-a",
    config: {},
  };
}

function group(over: Record<string, unknown> = {}) {
  return {
    id: "g1",
    community_id: CID,
    kind: "op",
    name: "Admins",
    players: [{ uuid: "u1", username: "alice" }],
    ...over,
  };
}

// Route `api.get` by path: the detail page reads the server object, the Players
// tab reads the two group lists.
function routeGet(opts: {
  attached?: unknown[];
  community?: unknown[];
  attachedError?: unknown;
}) {
  mockApi.get.mockImplementation((path: string) => {
    if (path.endsWith(`/servers/${SID}/groups`)) {
      if (opts.attachedError !== undefined) {
        return Promise.reject(opts.attachedError);
      }
      return Promise.resolve(opts.attached ?? []);
    }
    if (path === `/api/communities/${CID}/groups`) {
      return Promise.resolve(opts.community ?? []);
    }
    // Bare server detail fetch.
    return Promise.resolve(serverResponse());
  });
}

function renderTab() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={[`/communities/${CID}/servers/${SID}`]}>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <Routes>
            <Route
              path="/communities/:cid/servers/:sid"
              element={<ServerDetailPage />}
            />
          </Routes>
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

async function openPlayers() {
  await screen.findByText("survival");
  fireEvent.click(
    screen.getByRole("tab", { name: t("serverDetail.tab.players") }),
  );
}

describe("ServerPlayersTab", () => {
  // Keep the events socket "connected" so it never invalidates the detail
  // query out from under the path-routed get mock (missing WS mocks flaked CI).
  let restoreWs: () => void;

  beforeEach(() => {
    restoreWs = installMockWebSocket();
    setAccessToken("tok-1");
    mockApi.get.mockReset();
    mockApi.post.mockReset();
    mockApi.patch.mockReset();
    mockApi.put.mockReset();
    mockApi.delete.mockReset();
    mockCan = () => true;
  });
  afterEach(() => {
    restoreWs();
    vi.clearAllMocks();
  });

  it("lists attached groups with kind badges and member counts", async () => {
    routeGet({
      attached: [
        group({ id: "g1", kind: "op", name: "Admins", players: [{}, {}] }),
        group({ id: "g2", kind: "whitelist", name: "Friends", players: [{}] }),
      ],
    });
    renderTab();
    await openPlayers();

    expect(await screen.findByText("Admins")).toBeInTheDocument();
    expect(screen.getByText(t("players.kind.op"))).toBeInTheDocument();
    expect(screen.getByText(t("players.kind.whitelist"))).toBeInTheDocument();
    // Member counts come from the group's player-list length.
    expect(
      screen.getByText(`2 ${t("players.memberCount")}`),
    ).toBeInTheDocument();
    expect(
      screen.getByText(`1 ${t("players.memberCount")}`),
    ).toBeInTheDocument();
  });

  it("shows the empty state when no groups are attached", async () => {
    routeGet({ attached: [], community: [] });
    renderTab();
    await openPlayers();

    expect(await screen.findByText(t("players.empty"))).toBeInTheDocument();
  });

  it("shows a no-groups picker message when the community has zero groups", async () => {
    routeGet({ attached: [], community: [] });
    renderTab();
    await openPlayers();

    expect(
      await screen.findByText(t("players.attachNoGroups")),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(t("players.attachEmpty")),
    ).not.toBeInTheDocument();
  });

  it("does not flash the no-groups picker message while the community query is loading", async () => {
    // attached resolves immediately; community never settles, so community.data
    // stays undefined. The picker must show the loading message, not the
    // zero-groups empty state (#665).
    mockApi.get.mockImplementation((path: string) => {
      if (path.endsWith(`/servers/${SID}/groups`)) {
        return Promise.resolve([]);
      }
      if (path === `/api/communities/${CID}/groups`) {
        return new Promise(() => {});
      }
      return Promise.resolve(serverResponse());
    });
    renderTab();
    await openPlayers();

    expect(
      await screen.findByText(t("players.attachHeading")),
    ).toBeInTheDocument();
    expect(screen.getByText(t("players.loading"))).toBeInTheDocument();
    expect(
      screen.queryByText(t("players.attachNoGroups")),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(t("players.attachEmpty")),
    ).not.toBeInTheDocument();
  });

  it("shows the all-attached picker message when groups exist but all are attached", async () => {
    routeGet({
      attached: [group({ id: "g1", name: "Admins" })],
      community: [group({ id: "g1", name: "Admins" })],
    });
    renderTab();
    await openPlayers();

    expect(
      await screen.findByText(t("players.attachEmpty")),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(t("players.attachNoGroups")),
    ).not.toBeInTheDocument();
  });

  it("attach picker excludes already-attached community groups", async () => {
    routeGet({
      attached: [group({ id: "g1", name: "Admins" })],
      community: [
        group({ id: "g1", name: "Admins" }),
        group({ id: "g2", name: "Builders" }),
      ],
    });
    renderTab();
    await openPlayers();

    await screen.findByText("Admins");
    // Only the not-yet-attached group is offered to attach.
    expect(screen.getByText("Builders")).toBeInTheDocument();
    const attachButtons = screen.getAllByRole("button", {
      name: t("players.attach"),
    });
    expect(attachButtons).toHaveLength(1);
  });

  it("attaches a group with a PUT to the attach route", async () => {
    routeGet({
      attached: [],
      community: [group({ id: "g2", name: "Builders" })],
    });
    mockApi.put.mockResolvedValue(undefined);
    renderTab();
    await openPlayers();

    fireEvent.click(
      await screen.findByRole("button", { name: t("players.attach") }),
    );
    await waitFor(() => {
      expect(mockApi.put).toHaveBeenCalledWith(
        `/api/communities/${CID}/groups/g2/servers/${SID}`,
      );
    });
  });

  it("detaches a group with a DELETE to the attach route", async () => {
    routeGet({
      attached: [group({ id: "g1", name: "Admins" })],
      community: [],
    });
    mockApi.delete.mockResolvedValue(undefined);
    renderTab();
    await openPlayers();

    fireEvent.click(
      await screen.findByRole("button", { name: t("players.detach") }),
    );
    await waitFor(() => {
      expect(mockApi.delete).toHaveBeenCalledWith(
        `/api/communities/${CID}/groups/g1/servers/${SID}`,
      );
    });
  });

  it("hides attach/detach controls without group:manage", async () => {
    mockCan = (code: string) => code !== "group:manage";
    routeGet({
      attached: [group({ id: "g1", name: "Admins" })],
      community: [group({ id: "g2", name: "Builders" })],
    });
    renderTab();
    await openPlayers();

    await screen.findByText("Admins");
    expect(
      screen.queryByRole("button", { name: t("players.detach") }),
    ).not.toBeInTheDocument();
    // The attach picker section is gated on group:manage too.
    expect(
      screen.queryByText(t("players.attachHeading")),
    ).not.toBeInTheDocument();
  });

  it("routes a 403 on detach through onForbidden (named-permission toast)", async () => {
    routeGet({
      attached: [group({ id: "g1", name: "Admins" })],
      community: [],
    });
    mockApi.delete.mockRejectedValue(
      new ApiError(403, { reason: "forbidden", permission: "group:manage" }),
    );
    renderTab();
    await openPlayers();

    fireEvent.click(
      await screen.findByRole("button", { name: t("players.detach") }),
    );
    expect(
      await screen.findByText(`${t("permissions.deniedNamed")}group:manage`),
    ).toBeInTheDocument();
  });
});
