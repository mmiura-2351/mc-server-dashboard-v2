import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { setAccessToken } from "../auth/tokenStore.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { installMockWebSocket, MockWebSocket } from "../test/mockWebSocket.ts";
import { DashboardPage } from "./DashboardPage.tsx";
import { serversKey } from "./useCommunityEvents.ts";

const CID = "c1";

// Stub WebSocket because the dashboard opens its event stream on mount.
let restoreWebSocket: () => void;

const mockApi = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
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
vi.mock("../permissions/useCan.ts", () => ({
  useCan: () => mockCan,
}));

function server(overrides: Record<string, unknown> = {}) {
  return {
    id: "s1",
    community_id: CID,
    name: "survival",
    server_type: "paper",
    mc_edition: "java",
    mc_version: "1.21.6",
    game_port: 25565,
    desired_state: "running",
    observed_state: "running",
    observed_at: null,
    assigned_worker_id: "worker-a",
    config: {},
    join_hostname: null,
    bedrock_address: null,
    bedrock_port: null,
    ...overrides,
  };
}

// Mount under a community URL because the page derives its active target from :cid.
function renderPage(path = `/communities/${CID}`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <Routes>
            <Route path="/communities/:cid" element={<DashboardPage />} />
          </Routes>
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { ...result, queryClient };
}

function LocationProbe({ onChange }: { onChange: (search: string) => void }) {
  const location = useLocation();
  onChange(location.search);
  return null;
}

beforeEach(() => {
  restoreWebSocket = installMockWebSocket();
  setAccessToken("tok-1");
  mockApi.get.mockReset();
  mockApi.post.mockReset();
  mockCan = () => true;
  localStorage.clear();
});

afterEach(() => {
  restoreWebSocket();
  vi.clearAllMocks();
});

describe("DashboardPage list", () => {
  it("renders server cards with badges, port, worker and the state pill", async () => {
    mockApi.get.mockResolvedValue([server()]);
    renderPage();

    expect(await screen.findByText("survival")).toBeInTheDocument();
    expect(screen.getByText("paper 1.21.6")).toBeInTheDocument();
    expect(screen.getByText(":25565")).toBeInTheDocument();
    expect(
      screen.getByText(`${t("dashboard.col.worker")}: worker`),
    ).toBeInTheDocument();
    expect(screen.getAllByText(t("dashboard.state.running"))).toHaveLength(1);
  });

  it("shows the unknown pill for an unrecognised observed state", async () => {
    mockApi.get.mockResolvedValue([server({ observed_state: "bogus" })]);
    renderPage();

    expect(
      await screen.findByText(t("dashboard.state.unknown")),
    ).toBeInTheDocument();
  });

  it("renders the no-worker fallback when unassigned", async () => {
    mockApi.get.mockResolvedValue([
      server({ assigned_worker_id: null, observed_state: "stopped" }),
    ]);
    renderPage();

    expect(
      await screen.findByText(t("dashboard.noWorker")),
    ).toBeInTheDocument();
  });

  it("surfaces a load error", async () => {
    mockApi.get.mockRejectedValue(
      new ApiError(500, { reason: "internal_error" }),
    );
    renderPage();

    expect(
      await screen.findByText(t("dashboard.loadError")),
    ).toBeInTheDocument();
  });

  it("keeps rendering cached servers when a background refetch fails (#1724)", async () => {
    mockApi.get.mockResolvedValue([server()]);
    const { queryClient } = renderPage();
    await screen.findByText("survival");

    mockApi.get.mockRejectedValue(
      new ApiError(500, { reason: "internal_error" }),
    );
    await act(() => queryClient.invalidateQueries());
    // Flush the task that delivers query-state notifications after invalidation settles.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByText("survival")).toBeInTheDocument();
    expect(
      screen.queryByText(t("dashboard.loadError")),
    ).not.toBeInTheDocument();
  });

  it("recovers to fresh data once a refetch succeeds after a failure (#1724)", async () => {
    mockApi.get.mockResolvedValue([server()]);
    const { queryClient } = renderPage();
    await screen.findByText("survival");

    mockApi.get.mockRejectedValue(
      new ApiError(500, { reason: "internal_error" }),
    );
    await act(() => queryClient.invalidateQueries());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    mockApi.get.mockResolvedValue([server({ name: "creative" })]);
    await act(() => queryClient.invalidateQueries());

    expect(await screen.findByText("creative")).toBeInTheDocument();
    expect(
      screen.queryByText(t("dashboard.loadError")),
    ).not.toBeInTheDocument();
  });
});

describe("DashboardPage empty state", () => {
  it("shows the empty CTA linking to the create route", async () => {
    mockApi.get.mockResolvedValue([]);
    renderPage();

    expect(await screen.findByText(t("dashboard.empty"))).toBeInTheDocument();
    const cta = screen.getByRole("link", {
      name: t("dashboard.createServer"),
    });
    expect(cta).toHaveAttribute("href", `/communities/${CID}/servers/new`);
  });
});

describe("DashboardPage community-not-found (#784)", () => {
  it("shows the not-found state for a URL cid outside the membership list", async () => {
    renderPage("/communities/other");

    expect(
      await screen.findByText(t("community.notFound.title")),
    ).toBeInTheDocument();
    expect(screen.getByText(t("community.notFound.body"))).toBeInTheDocument();
    expect(mockApi.get).not.toHaveBeenCalled();
  });
});

describe("DashboardPage permission-gated actions", () => {
  it("renders only the actions the caller may perform", async () => {
    mockCan = (code) => code === "server:stop";
    mockApi.get.mockResolvedValue([server({ observed_state: "running" })]);
    renderPage();

    await screen.findByText("survival");
    expect(
      screen.getByRole("button", { name: t("dashboard.stop") }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("dashboard.restart") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("dashboard.start") }),
    ).not.toBeInTheDocument();
  });

  it("disables an action that does not apply to the current state", async () => {
    mockApi.get.mockResolvedValue([
      server({ observed_state: "stopped", desired_state: "stopped" }),
    ]);
    renderPage();

    await screen.findByText("survival");
    expect(
      screen.getByRole("button", { name: t("dashboard.start") }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: t("dashboard.stop") }),
    ).toBeDisabled();
  });
});

describe("DashboardPage lifecycle actions", () => {
  it("starts a stopped server and invalidates the list on settle", async () => {
    mockApi.get.mockResolvedValue([
      server({ observed_state: "stopped", desired_state: "stopped" }),
    ]);
    mockApi.post.mockResolvedValue(server({ observed_state: "starting" }));
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: t("dashboard.start") }));

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/s1/start`,
      ),
    );
    await waitFor(() => expect(mockApi.get).toHaveBeenCalledTimes(2));
  });

  it("routes a 403 through the permission glue, not a generic toast", async () => {
    mockApi.get.mockResolvedValue([server({ observed_state: "running" })]);
    mockApi.post.mockRejectedValue(
      new ApiError(403, { reason: "forbidden", permission: "server:stop" }),
    );
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: t("dashboard.stop") }));

    expect(
      await screen.findByText(
        t("permissions.deniedNamed", { permission: "server:stop" }),
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(t("dashboard.actionFailed")),
    ).not.toBeInTheDocument();
  });

  it("gives a 409 the state-changed treatment and refetches", async () => {
    mockApi.get.mockResolvedValue([server({ observed_state: "running" })]);
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "transition_conflict" }),
    );
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: t("dashboard.stop") }));

    expect(
      await screen.findByText(t("dashboard.stateChanged")),
    ).toBeInTheDocument();
    await waitFor(() => expect(mockApi.get).toHaveBeenCalledTimes(2));
  });

  it("surfaces a specific message for a 409 port_conflict start failure", async () => {
    mockApi.get.mockResolvedValue([
      server({ observed_state: "stopped", desired_state: "stopped" }),
    ]);
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "port_conflict" }),
    );
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: t("dashboard.start") }));

    expect(
      await screen.findByText(t("dashboard.lifecycle.portConflict")),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(t("dashboard.stateChanged")),
    ).not.toBeInTheDocument();
  });

  it("surfaces a specific message for a 409 image_missing start failure", async () => {
    mockApi.get.mockResolvedValue([
      server({ observed_state: "stopped", desired_state: "stopped" }),
    ]);
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "image_missing" }),
    );
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: t("dashboard.start") }));

    expect(
      await screen.findByText(t("dashboard.lifecycle.imageMissing")),
    ).toBeInTheDocument();
  });

  it("falls back to a generic toast for other errors", async () => {
    mockApi.get.mockResolvedValue([server({ observed_state: "running" })]);
    mockApi.post.mockRejectedValue(
      new ApiError(500, { reason: "internal_error" }),
    );
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: t("dashboard.stop") }));

    expect(
      await screen.findByText(t("dashboard.actionFailed")),
    ).toBeInTheDocument();
  });

  it("labels the Start button as Restart when the server is crashed", async () => {
    mockApi.get.mockResolvedValue([
      server({
        observed_state: "crashed",
        desired_state: "stopped",
      }),
    ]);
    renderPage();

    await screen.findByText("survival");
    const restartButtons = screen.getAllByRole("button", {
      name: t("dashboard.startCrashed"),
    });
    const startButton = restartButtons.find((btn) =>
      btn.className.includes("success"),
    );
    expect(startButton).toBeDefined();
  });

  it("drives only the clicked row through the optimistic lifecycle", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "alpha" }),
      server({ id: "s2", name: "bravo" }),
    ]);
    let rejectPost!: (err: unknown) => void;
    mockApi.post.mockReturnValue(
      new Promise((_resolve, reject) => {
        rejectPost = reject;
      }),
    );
    const { queryClient } = renderPage();

    const alpha = (await screen.findByText("alpha")).closest(
      ".server-card",
    ) as HTMLElement;
    const bravo = screen
      .getByText("bravo")
      .closest(".server-card") as HTMLElement;
    fireEvent.click(
      within(bravo).getByRole("button", { name: t("dashboard.stop") }),
    );

    expect(
      await within(bravo).findByText(t("dashboard.state.stopping")),
    ).toBeInTheDocument();
    expect(mockApi.post).toHaveBeenCalledWith(
      `/api/communities/${CID}/servers/s2/stop`,
    );
    expect(
      queryClient
        .getQueryData<{ id: string; observed_state: string }[]>(serversKey(CID))
        ?.map((s) => s.observed_state),
    ).toEqual(["running", "stopping"]);
    expect(
      within(bravo).getByRole("button", { name: t("dashboard.restart") }),
    ).toBeDisabled();
    expect(within(alpha).getByText(t("dashboard.state.running"))).toBeVisible();
    expect(
      within(alpha).getByRole("button", { name: t("dashboard.stop") }),
    ).toBeEnabled();

    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message({
        stream: "status",
        ts: "t",
        payload: { state: "crashed", detail: "" },
        server_id: "s2",
      });
    });
    expect(
      within(bravo).getByRole("button", { name: t("dashboard.stop") }),
    ).toBeDisabled();

    // Keep refetch pending so the assertion observes the live cache update.
    mockApi.get.mockReturnValue(new Promise(() => {}));
    act(() => rejectPost(new ApiError(409, { reason: "worker_busy" })));

    expect(
      await screen.findByText(t("dashboard.lifecycle.stopPending")),
    ).toBeInTheDocument();
    expect(within(bravo).getByText(t("dashboard.state.crashed"))).toBeVisible();
    expect(
      within(bravo).getByRole("button", { name: t("dashboard.stop") }),
    ).toBeEnabled();
  });
});

describe("DashboardPage live status", () => {
  it("patches a card's pill from a status event without a refetch", async () => {
    mockApi.get.mockResolvedValue([server({ observed_state: "stopped" })]);
    renderPage();

    await screen.findByText("survival");
    expect(screen.getAllByText(t("dashboard.state.stopped"))).toHaveLength(1);

    const socket = MockWebSocket.last();
    socket.open();
    socket.message({
      stream: "status",
      ts: "t",
      payload: { state: "running", detail: "" },
      server_id: "s1",
    });

    await waitFor(() =>
      expect(screen.getAllByText(t("dashboard.state.running"))).toHaveLength(1),
    );
    expect(mockApi.get).toHaveBeenCalledTimes(1);
  });

  it("shows the live-degraded indicator on WS failure", async () => {
    mockApi.get.mockResolvedValue([server({ observed_state: "running" })]);
    renderPage();

    await screen.findByText("survival");
    expect(
      screen.queryByText(t("dashboard.liveDegraded")),
    ).not.toBeInTheDocument();

    MockWebSocket.last().fail();
    expect(
      await screen.findByText(t("dashboard.liveDegraded")),
    ).toBeInTheDocument();
  });
});

describe("DashboardPage view toggle (#541)", () => {
  it("defaults to cards and switches to the table view on toggle", async () => {
    mockApi.get.mockResolvedValue([server()]);
    renderPage();

    await screen.findByText("survival");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    expect(
      screen.getByRole("columnheader", { name: /Name/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("persists the chosen view across reloads via localStorage", async () => {
    mockApi.get.mockResolvedValue([server()]);
    const first = renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );
    expect(screen.getByRole("table")).toBeInTheDocument();
    first.unmount();

    renderPage();
    await screen.findByText("survival");
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("shows the same servers and data in the table as the cards", async () => {
    mockApi.get.mockResolvedValue([
      server(),
      server({ id: "s2", name: "creative" }),
    ]);
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    expect(screen.getByText("survival")).toBeInTheDocument();
    expect(screen.getByText("creative")).toBeInTheDocument();
    expect(screen.getAllByText(t("dashboard.state.running"))).toHaveLength(2);
    expect(screen.getAllByText("paper 1.21.6")).toHaveLength(2);
    expect(screen.getAllByText("25565")).toHaveLength(2);
    expect(screen.getAllByText("worker")).toHaveLength(2);
  });

  it("runs a quick action from the table row", async () => {
    mockApi.get.mockResolvedValue([
      server({ observed_state: "stopped", desired_state: "stopped" }),
    ]);
    mockApi.post.mockResolvedValue(server({ observed_state: "starting" }));
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );
    fireEvent.click(screen.getByRole("button", { name: t("dashboard.start") }));

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/s1/start`,
      ),
    );
  });
});

describe("DashboardPage server addresses (issues #982, #1543)", () => {
  it("table column header is 'Address', not 'Port'", async () => {
    mockApi.get.mockResolvedValue([server()]);
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    expect(screen.getByText(t("dashboard.col.address"))).toBeInTheDocument();
    expect(screen.queryByText(t("dashboard.col.port"))).not.toBeInTheDocument();
  });

  it("wires each server's Java and Bedrock addresses into its card and table row", async () => {
    mockCan = () => false;
    mockApi.get.mockResolvedValue([
      server({
        join_hostname: "survival.relay.example.com",
        game_port: 25565,
        bedrock_address: "play.example.com",
        bedrock_port: 19132,
      }),
      server({ id: "s2", name: "creative", game_port: 25566 }),
    ]);
    renderPage();
    const bedrockName = `${t("dashboard.bedrockLabel")}: play.example.com:19132`;

    const javaBadge = await screen.findByRole("button", {
      name: "survival.relay.example.com",
    });
    expect(javaBadge).toHaveAttribute("class", "badge copyable");
    expect(screen.getByRole("button", { name: bedrockName })).toHaveAttribute(
      "class",
      "badge copyable",
    );
    expect(screen.queryByText(":25565")).not.toBeInTheDocument();
    expect(screen.getByText(":25566").tagName).toBe("SPAN");

    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    expect(
      screen.getByRole("button", { name: "survival.relay.example.com" }),
    ).toHaveAttribute("class", "copyable");
    expect(screen.getByRole("button", { name: bedrockName })).toHaveAttribute(
      "class",
      "copyable",
    );
    expect(screen.queryByText("25565")).not.toBeInTheDocument();
    expect(screen.getByText("25566")).toBeInTheDocument();
  });
});

describe("DashboardPage filter and sort (#1123)", () => {
  it("filters servers by name search", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "survival" }),
      server({ id: "s2", name: "creative" }),
    ]);
    renderPage();

    await screen.findByText("survival");
    expect(screen.getByText("creative")).toBeInTheDocument();

    const searchInput = screen.getByPlaceholderText(
      t("dashboard.filter.search"),
    );
    fireEvent.change(searchInput, { target: { value: "surv" } });

    expect(screen.getByText("survival")).toBeInTheDocument();
    expect(screen.queryByText("creative")).not.toBeInTheDocument();
  });

  it("filters servers by state bucket selection", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "survival", observed_state: "running" }),
      server({
        id: "s2",
        name: "creative",
        observed_state: "stopped",
        desired_state: "stopped",
      }),
    ]);
    renderPage();

    await screen.findByText("survival");
    expect(screen.getByText("creative")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.filter.state") }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: t("dashboard.filter.bucket.running"),
      }),
    );

    expect(screen.getByText("survival")).toBeInTheDocument();
    expect(screen.queryByText("creative")).not.toBeInTheDocument();
  });

  it("its bucket covers a server in an unknown observed state (#2239)", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "survival", observed_state: "running" }),
      server({ id: "s2", name: "mystery", observed_state: "bogus" }),
    ]);
    renderPage();

    await screen.findByText("mystery");

    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.filter.state") }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: t("dashboard.filter.bucket.other"),
      }),
    );

    expect(screen.getByText("mystery")).toBeInTheDocument();
    expect(screen.queryByText("survival")).not.toBeInTheDocument();
  });

  it("shows the selected-bucket count badge only when at least one is picked", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "survival", observed_state: "running" }),
    ]);
    renderPage();

    await screen.findByText("survival");
    const trigger = screen.getByRole("button", {
      name: t("dashboard.filter.state"),
    });
    expect(trigger).not.toHaveTextContent("1");

    fireEvent.click(trigger);
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: t("dashboard.filter.bucket.running"),
      }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: t("dashboard.filter.bucket.stopped"),
      }),
    );
    expect(trigger).toHaveTextContent("2");

    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.filter.clear") }),
    );
    expect(trigger).not.toHaveTextContent("2");
  });

  it("folds the selected-bucket count into the trigger's accessible name (#2668)", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "survival", observed_state: "running" }),
    ]);
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.filter.state") }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: t("dashboard.filter.bucket.running"),
      }),
    );

    await act(async () => {});
    expect(
      screen.getByRole("button", {
        name: t("dashboard.filter.stateCount", { count: 1 }),
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("dashboard.filter.state") }),
    ).not.toBeInTheDocument();
  });

  it("ignores a stale non-bucket state token in the URL param (#2668)", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "survival", observed_state: "running" }),
      server({
        id: "s2",
        name: "creative",
        observed_state: "stopped",
        desired_state: "stopped",
      }),
    ]);
    // Legacy bookmarks may contain raw state names that are not current bucket keys.
    const { container } = renderPage(`/communities/${CID}?state=starting`);

    await screen.findByText("survival");
    expect(screen.getByText("creative")).toBeInTheDocument();
    const trigger = container.querySelector(".filter-state-trigger");
    expect(trigger).not.toHaveTextContent("1");
  });

  it("counts and filters on only the valid tokens when the param mixes valid and stale (#2668)", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "survival", observed_state: "running" }),
      server({
        id: "s2",
        name: "creative",
        observed_state: "stopped",
        desired_state: "stopped",
      }),
    ]);
    const { container } = renderPage(
      `/communities/${CID}?state=running,starting`,
    );

    await screen.findByText("survival");
    expect(screen.queryByText("creative")).not.toBeInTheDocument();
    const trigger = container.querySelector(".filter-state-trigger");
    expect(trigger).toHaveTextContent("1");
    expect(trigger).not.toHaveTextContent("2");
  });

  it("round-trips the selected buckets through the state URL param", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "survival", observed_state: "running" }),
    ]);
    let search = "";
    render(
      <MemoryRouter initialEntries={[`/communities/${CID}`]}>
        <QueryClientProvider
          client={
            new QueryClient({
              defaultOptions: {
                queries: { retry: false },
                mutations: { retry: false },
              },
            })
          }
        >
          <ToastProvider>
            <Routes>
              <Route path="/communities/:cid" element={<DashboardPage />} />
            </Routes>
            <LocationProbe onChange={(s) => (search = s)} />
          </ToastProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.filter.state") }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: t("dashboard.filter.bucket.crashed"),
      }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: t("dashboard.filter.bucket.running"),
      }),
    );

    expect(new URLSearchParams(search).get("state")).toBe("running,crashed");
  });

  it("shows the empty-filter message when no servers match", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "survival", observed_state: "running" }),
    ]);
    renderPage();

    await screen.findByText("survival");

    const searchInput = screen.getByPlaceholderText(
      t("dashboard.filter.search"),
    );
    fireEvent.change(searchInput, { target: { value: "nonexistent" } });

    expect(screen.getByText(t("dashboard.filter.noMatch"))).toBeInTheDocument();
    expect(screen.queryByText("survival")).not.toBeInTheDocument();
  });

  it("sorts servers by name ascending by default", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s2", name: "creative" }),
      server({ id: "s1", name: "survival" }),
    ]);
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    const rows = screen.getAllByRole("row");
    expect(rows[1]).toHaveTextContent("creative");
    expect(rows[2]).toHaveTextContent("survival");
  });

  it("clicking a table column header sorts by that field", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "survival", observed_state: "running" }),
      server({
        id: "s2",
        name: "creative",
        observed_state: "stopped",
        desired_state: "stopped",
      }),
    ]);
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    fireEvent.click(screen.getByRole("columnheader", { name: /State/ }));

    const rows = screen.getAllByRole("row");
    expect(rows[1]).toHaveTextContent("survival");
    expect(rows[2]).toHaveTextContent("creative");
  });

  it("clicking the same column header toggles sort direction", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "alpha" }),
      server({ id: "s2", name: "zulu" }),
    ]);
    renderPage();

    await screen.findByText("alpha");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    let rows = screen.getAllByRole("row");
    expect(rows[1]).toHaveTextContent("alpha");
    expect(rows[2]).toHaveTextContent("zulu");

    fireEvent.click(screen.getByRole("columnheader", { name: /Name/ }));
    rows = screen.getAllByRole("row");
    expect(rows[1]).toHaveTextContent("zulu");
    expect(rows[2]).toHaveTextContent("alpha");
  });

  it("persists sort preference in localStorage", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "alpha" }),
      server({ id: "s2", name: "zulu" }),
    ]);
    const first = renderPage();

    await screen.findByText("alpha");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );
    fireEvent.click(screen.getByRole("columnheader", { name: /Name/ }));
    let rows = screen.getAllByRole("row");
    expect(rows[1]).toHaveTextContent("zulu");
    first.unmount();

    renderPage();
    await screen.findByText("alpha");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );
    rows = screen.getAllByRole("row");
    expect(rows[1]).toHaveTextContent("zulu");
    expect(rows[2]).toHaveTextContent("alpha");
  });

  it("card view shows a sort control with the current sort field", async () => {
    mockApi.get.mockResolvedValue([server()]);
    renderPage();

    await screen.findByText("survival");
    expect(screen.getByRole("button", { name: /Name.*▲/ })).toBeInTheDocument();
  });

  it("filter and sort work together", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "beta-survival", observed_state: "running" }),
      server({ id: "s2", name: "alpha-creative", observed_state: "running" }),
      server({
        id: "s3",
        name: "gamma-lobby",
        observed_state: "stopped",
        desired_state: "stopped",
      }),
    ]);
    renderPage();

    await screen.findByText("beta-survival");

    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.filter.state") }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: t("dashboard.filter.bucket.running"),
      }),
    );

    expect(screen.queryByText("gamma-lobby")).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    const rows = screen.getAllByRole("row");
    expect(rows[1]).toHaveTextContent("alpha-creative");
    expect(rows[2]).toHaveTextContent("beta-survival");
  });

  it("does not show filter controls when server list is empty", async () => {
    mockApi.get.mockResolvedValue([]);
    renderPage();

    await screen.findByText(t("dashboard.empty"));
    expect(
      screen.queryByPlaceholderText(t("dashboard.filter.search")),
    ).not.toBeInTheDocument();
  });

  it("initializes filters from URL query params", async () => {
    mockApi.get.mockResolvedValue([
      server({ id: "s1", name: "survival", observed_state: "running" }),
      server({
        id: "s2",
        name: "creative",
        observed_state: "stopped",
        desired_state: "stopped",
      }),
    ]);
    renderPage(`/communities/${CID}?search=surv&state=running`);

    await screen.findByText("survival");
    const searchInput = screen.getByPlaceholderText(
      t("dashboard.filter.search"),
    );
    expect(searchInput).toHaveValue("surv");
    expect(screen.queryByText("creative")).not.toBeInTheDocument();
  });
});

describe("DashboardPage desired/observed drift (issue #2443)", () => {
  it("marks a drifting row with the same affordance the detail page shows (cards view)", async () => {
    mockApi.get.mockResolvedValue([
      server({ observed_state: "running", desired_state: "stopped" }),
    ]);
    renderPage();

    expect(await screen.findByText("survival")).toBeInTheDocument();
    expect(screen.getByText(t("dashboard.state.running"))).toBeInTheDocument();
    expect(screen.getByText(t("serverDetail.converging"))).toBeInTheDocument();
  });

  it("does not mark a settled row where desired equals observed (cards view)", async () => {
    mockApi.get.mockResolvedValue([
      server({ observed_state: "running", desired_state: "running" }),
    ]);
    renderPage();

    await screen.findByText("survival");
    // Flush query notifications before asserting absence so a delayed render cannot hide a regression.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(
      screen.queryByText(t("serverDetail.converging")),
    ).not.toBeInTheDocument();
  });

  it("marks a drifting row in the table view too", async () => {
    mockApi.get.mockResolvedValue([
      server({ observed_state: "running", desired_state: "stopped" }),
    ]);
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    expect(screen.getByText(t("serverDetail.converging"))).toBeInTheDocument();
  });

  it("keeps the drift mark on a drifting row that passes the observed-state bucket filter", async () => {
    mockApi.get.mockResolvedValue([
      server({
        id: "s1",
        name: "survival",
        observed_state: "running",
        desired_state: "stopped",
      }),
      server({
        id: "s2",
        name: "creative",
        observed_state: "stopped",
        desired_state: "stopped",
      }),
    ]);
    renderPage();

    await screen.findByText("survival");

    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.filter.state") }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: t("dashboard.filter.bucket.running"),
      }),
    );

    expect(screen.getByText("survival")).toBeInTheDocument();
    expect(screen.queryByText("creative")).not.toBeInTheDocument();
    expect(screen.getByText(t("serverDetail.converging"))).toBeInTheDocument();
  });
});
