import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { setAccessToken } from "../auth/tokenStore.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { installMockWebSocket, MockWebSocket } from "../test/mockWebSocket.ts";
import { DashboardPage } from "./DashboardPage.tsx";

const CID = "c1";

// The dashboard mounts the live community-events WS; back it with the mock so
// jsdom (which has no WebSocket) does not throw on render.
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

// A controllable active community + permission resolver per test.
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

// The page derives its community from the URL `:cid` (#784), so mount it under a
// matching route. Default to the member community; pass another cid to exercise
// the not-found state for a community outside the caller's membership.
function renderPage(path = `/communities/${CID}`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
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
}

beforeEach(() => {
  restoreWebSocket = installMockWebSocket();
  setAccessToken("tok-1");
  mockApi.get.mockReset();
  mockApi.post.mockReset();
  mockCan = () => true;
  // The view toggle persists in localStorage; start each test from the default.
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
    // The worker chip is labelled and the id abbreviated (#644): "worker-a"
    // shortens to its leading segment.
    expect(
      screen.getByText(`${t("dashboard.col.worker")}: worker`),
    ).toBeInTheDocument();
    // The filter bar renders a "running" chip; the server card renders another.
    expect(screen.getAllByText(t("dashboard.state.running"))).toHaveLength(2);
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
      new ApiError(500, { reason: "server_error" }),
    );
    renderPage();

    expect(
      await screen.findByText(t("dashboard.loadError")),
    ).toBeInTheDocument();
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
    // It must not silently fall back to listing the member community's servers.
    expect(mockApi.get).not.toHaveBeenCalled();
  });
});

describe("DashboardPage permission-gated actions", () => {
  it("renders only the actions the caller may perform", async () => {
    // A running server: stop + restart apply. Permit stop only.
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
    // Stopped server: stop does not apply even though it is permitted.
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
    // The list refetches after the action settles.
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
      new ApiError(409, { reason: "server_unsettled" }),
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
      new ApiError(500, { reason: "server_error" }),
    );
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: t("dashboard.stop") }));

    expect(
      await screen.findByText(t("dashboard.actionFailed")),
    ).toBeInTheDocument();
  });

  it("optimistically shows the transitional pill immediately on start", async () => {
    // The mutation never resolves during this test — the pill must already
    // show "Starting" before the API responds.
    mockApi.get.mockResolvedValue([
      server({ observed_state: "stopped", desired_state: "stopped" }),
    ]);
    mockApi.post.mockReturnValue(new Promise(() => {}));
    renderPage();

    // The filter bar always shows a "stopped" chip; the server card adds another.
    await waitFor(() =>
      expect(screen.getAllByText(t("dashboard.state.stopped"))).toHaveLength(2),
    );
    fireEvent.click(screen.getByRole("button", { name: t("dashboard.start") }));

    // After the action, the server's pill changes to "starting" (now 2: chip + pill).
    // "stopped" drops to 1 (the filter chip only).
    await waitFor(() =>
      expect(screen.getAllByText(t("dashboard.state.starting"))).toHaveLength(
        2,
      ),
    );
    expect(screen.getAllByText(t("dashboard.state.stopped"))).toHaveLength(1);
  });

  it("optimistically shows the transitional pill immediately on stop", async () => {
    mockApi.get.mockResolvedValue([server({ observed_state: "running" })]);
    mockApi.post.mockReturnValue(new Promise(() => {}));
    renderPage();

    await waitFor(() =>
      expect(screen.getAllByText(t("dashboard.state.running"))).toHaveLength(2),
    );
    fireEvent.click(screen.getByRole("button", { name: t("dashboard.stop") }));

    await waitFor(() =>
      expect(screen.getAllByText(t("dashboard.state.stopping"))).toHaveLength(
        2,
      ),
    );
    expect(screen.getAllByText(t("dashboard.state.running"))).toHaveLength(1);
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
    // Both the start and restart buttons carry the "Restart" label, but only
    // the start button has the `.success` class. Find the success-styled one
    // to confirm the start action was relabeled.
    const restartButtons = screen.getAllByRole("button", {
      name: t("dashboard.startCrashed"),
    });
    const startButton = restartButtons.find((btn) =>
      btn.className.includes("success"),
    );
    expect(startButton).toBeDefined();
  });

  it("reverts the pill to the previous state on error", async () => {
    mockApi.get.mockResolvedValue([server({ observed_state: "stopped" })]);
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "port_conflict" }),
    );
    renderPage();

    await waitFor(() =>
      expect(screen.getAllByText(t("dashboard.state.stopped"))).toHaveLength(2),
    );
    fireEvent.click(screen.getByRole("button", { name: t("dashboard.start") }));

    // After the error, the pill reverts to "Stopped" (filter chip + server pill = 2).
    await waitFor(() =>
      expect(screen.getAllByText(t("dashboard.state.stopped"))).toHaveLength(2),
    );
  });
});

describe("DashboardPage live status", () => {
  it("patches a card's pill from a status event without a refetch", async () => {
    mockApi.get.mockResolvedValue([server({ observed_state: "stopped" })]);
    renderPage();

    await screen.findByText("survival");
    // Filter bar chip + server pill = 2 "stopped" matches.
    expect(screen.getAllByText(t("dashboard.state.stopped"))).toHaveLength(2);

    const socket = MockWebSocket.last();
    socket.open();
    socket.message({
      stream: "status",
      ts: "t",
      payload: { state: "running", detail: "" },
      server_id: "s1",
    });

    // After the status event, the server pill changes to "running" (chip + pill = 2).
    await waitFor(() =>
      expect(screen.getAllByText(t("dashboard.state.running"))).toHaveLength(2),
    );
    // No second list fetch: the cache was patched in place.
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
    // Cards by default: no table role rendered.
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    // The table view exposes column headers and the same row data.
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

    // A fresh mount (simulating a reload) restores the table view.
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

    // Both servers, plus the shared card data: state pill, type/version,
    // port, worker. The filter bar adds one more "running" chip.
    expect(screen.getByText("survival")).toBeInTheDocument();
    expect(screen.getByText("creative")).toBeInTheDocument();
    expect(screen.getAllByText(t("dashboard.state.running"))).toHaveLength(3);
    expect(screen.getAllByText("paper 1.21.6")).toHaveLength(2);
    expect(screen.getAllByText("25565")).toHaveLength(2);
    // The worker id is abbreviated to its leading segment (#644).
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

describe("DashboardPage join address in server list (issue #982)", () => {
  it("shows the port badge when join_hostname is null (cards view)", async () => {
    mockApi.get.mockResolvedValue([
      server({ join_hostname: null, game_port: 25565 }),
    ]);
    renderPage();

    expect(await screen.findByText(":25565")).toBeInTheDocument();
  });

  it("shows hostname-only badge (no port) when join_hostname is set (cards view)", async () => {
    mockApi.get.mockResolvedValue([
      server({ join_hostname: "survival.relay.example.com", game_port: 25565 }),
    ]);
    renderPage();

    expect(
      await screen.findByText("survival.relay.example.com"),
    ).toBeInTheDocument();
    // Port badge must be hidden when relay is active.
    expect(screen.queryByText(":25565")).not.toBeInTheDocument();
  });

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

  it("shows port in the address cell when join_hostname is null (table view)", async () => {
    mockApi.get.mockResolvedValue([
      server({ join_hostname: null, game_port: 25565 }),
    ]);
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    expect(screen.getByText("25565")).toBeInTheDocument();
  });

  it("shows hostname in the address cell when join_hostname is set (table view)", async () => {
    mockApi.get.mockResolvedValue([
      server({
        join_hostname: "survival.relay.example.com",
        game_port: 25565,
      }),
    ]);
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    expect(screen.getByText("survival.relay.example.com")).toBeInTheDocument();
    // Port must not appear when relay is active.
    expect(screen.queryByText("25565")).not.toBeInTheDocument();
  });

  it("hostname badge is a clickable button in cards view", async () => {
    mockApi.get.mockResolvedValue([
      server({ join_hostname: "survival.relay.example.com", game_port: 25565 }),
    ]);
    renderPage();

    const badge = await screen.findByRole("button", {
      name: "survival.relay.example.com",
    });
    expect(badge).toBeInTheDocument();
    expect(badge.tagName).toBe("BUTTON");
    expect(badge).toHaveAttribute("title", "survival.relay.example.com");
  });

  it("clicking hostname badge copies via execCommand and shows Copied! (cards view)", async () => {
    mockApi.get.mockResolvedValue([
      server({ join_hostname: "survival.relay.example.com" }),
    ]);
    renderPage();
    await screen.findByText("survival.relay.example.com");

    if (!("execCommand" in document)) {
      Object.defineProperty(document, "execCommand", {
        value: () => true,
        writable: true,
        configurable: true,
      });
    }
    const execSpy = vi.spyOn(document, "execCommand").mockReturnValue(true);

    fireEvent.click(screen.getByText("survival.relay.example.com"));

    expect(execSpy).toHaveBeenCalledWith("copy");
    expect(
      await screen.findByText(t("dashboard.copiedJoinHostname")),
    ).toBeInTheDocument();

    execSpy.mockRestore();
  });

  it("hostname is a clickable button in table view", async () => {
    mockApi.get.mockResolvedValue([
      server({ join_hostname: "survival.relay.example.com", game_port: 25565 }),
    ]);
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    const btn = screen.getByRole("button", {
      name: "survival.relay.example.com",
    });
    expect(btn).toBeInTheDocument();
    expect(btn.tagName).toBe("BUTTON");
  });

  it("clicking hostname copies and shows Copied! in table view", async () => {
    mockApi.get.mockResolvedValue([
      server({ join_hostname: "survival.relay.example.com" }),
    ]);
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    if (!("execCommand" in document)) {
      Object.defineProperty(document, "execCommand", {
        value: () => true,
        writable: true,
        configurable: true,
      });
    }
    const execSpy = vi.spyOn(document, "execCommand").mockReturnValue(true);

    fireEvent.click(screen.getByText("survival.relay.example.com"));

    expect(execSpy).toHaveBeenCalledWith("copy");
    expect(
      await screen.findByText(t("dashboard.copiedJoinHostname")),
    ).toBeInTheDocument();

    execSpy.mockRestore();
  });

  it("port display is not clickable when join_hostname is null (cards view)", async () => {
    mockApi.get.mockResolvedValue([
      server({ join_hostname: null, game_port: 25565 }),
    ]);
    renderPage();

    const port = await screen.findByText(":25565");
    expect(port.tagName).toBe("SPAN");
  });
});

describe("DashboardPage Bedrock address badge (issue #1543)", () => {
  it("shows the Bedrock badge when bedrock_port is set (cards view)", async () => {
    mockApi.get.mockResolvedValue([
      server({ bedrock_address: "play.example.com", bedrock_port: 19132 }),
    ]);
    renderPage();

    const badge = await screen.findByRole("button", {
      name: `${t("dashboard.bedrockLabel")}: play.example.com:19132`,
    });
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAttribute("title", "play.example.com:19132");
  });

  it("hides the Bedrock badge when bedrock_port is null (cards view)", async () => {
    mockApi.get.mockResolvedValue([
      server({ bedrock_address: null, bedrock_port: null }),
    ]);
    renderPage();

    await screen.findByText("survival");
    expect(
      screen.queryByRole("button", {
        name: new RegExp(t("dashboard.bedrockLabel")),
      }),
    ).not.toBeInTheDocument();
  });

  it("Java badge is unchanged when the Bedrock badge is also shown (cards view)", async () => {
    mockApi.get.mockResolvedValue([
      server({
        join_hostname: "survival.relay.example.com",
        bedrock_address: "play.example.com",
        bedrock_port: 19132,
      }),
    ]);
    renderPage();

    expect(
      await screen.findByRole("button", {
        name: "survival.relay.example.com",
      }),
    ).toBeInTheDocument();
  });

  it("clicking the Bedrock badge copies address:port and shows Copied! (cards view)", async () => {
    mockApi.get.mockResolvedValue([
      server({ bedrock_address: "play.example.com", bedrock_port: 19132 }),
    ]);
    renderPage();
    const badge = await screen.findByRole("button", {
      name: `${t("dashboard.bedrockLabel")}: play.example.com:19132`,
    });

    if (!("execCommand" in document)) {
      Object.defineProperty(document, "execCommand", {
        value: () => true,
        writable: true,
        configurable: true,
      });
    }
    const execSpy = vi.spyOn(document, "execCommand").mockReturnValue(true);

    fireEvent.click(badge);

    expect(execSpy).toHaveBeenCalledWith("copy");
    expect(
      await screen.findByText(t("dashboard.copiedBedrockAddress")),
    ).toBeInTheDocument();

    execSpy.mockRestore();
  });

  it("shows the Bedrock address in the address cell (table view)", async () => {
    mockApi.get.mockResolvedValue([
      server({ bedrock_address: "play.example.com", bedrock_port: 19132 }),
    ]);
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    expect(
      await screen.findByRole("button", {
        name: `${t("dashboard.bedrockLabel")}: play.example.com:19132`,
      }),
    ).toBeInTheDocument();
  });

  it("Java and Bedrock copy states are independent (cards view)", async () => {
    mockApi.get.mockResolvedValue([
      server({
        join_hostname: "survival.relay.example.com",
        bedrock_address: "play.example.com",
        bedrock_port: 19132,
      }),
    ]);
    renderPage();
    const bedrockBadge = await screen.findByRole("button", {
      name: `${t("dashboard.bedrockLabel")}: play.example.com:19132`,
    });

    if (!("execCommand" in document)) {
      Object.defineProperty(document, "execCommand", {
        value: () => true,
        writable: true,
        configurable: true,
      });
    }
    const execSpy = vi.spyOn(document, "execCommand").mockReturnValue(true);

    // Copying the Bedrock address must not flip the Java badge to "Copied!".
    fireEvent.click(bedrockBadge);
    expect(
      await screen.findByText(t("dashboard.copiedBedrockAddress")),
    ).toBeInTheDocument();
    expect(screen.getByText("survival.relay.example.com")).toBeInTheDocument();
    // Both copied labels are the same literal, so pin exactly one is showing.
    expect(screen.getAllByText(t("dashboard.copiedJoinHostname"))).toHaveLength(
      1,
    );

    // And vice versa once the Bedrock badge has reverted (real 1500ms timer).
    await screen.findByRole(
      "button",
      { name: `${t("dashboard.bedrockLabel")}: play.example.com:19132` },
      { timeout: 3000 },
    );
    fireEvent.click(screen.getByText("survival.relay.example.com"));
    expect(
      await screen.findByText(t("dashboard.copiedJoinHostname")),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: `${t("dashboard.bedrockLabel")}: play.example.com:19132`,
      }),
    ).toBeInTheDocument();
    expect(screen.getAllByText(t("dashboard.copiedJoinHostname"))).toHaveLength(
      1,
    );

    execSpy.mockRestore();
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

  it("filters servers by state chip toggle", async () => {
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

    // Click the "running" filter chip to show only running servers.
    const runningChip = screen.getByRole("button", {
      name: t("dashboard.state.running"),
      pressed: false,
    });
    fireEvent.click(runningChip);

    expect(screen.getByText("survival")).toBeInTheDocument();
    expect(screen.queryByText("creative")).not.toBeInTheDocument();
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
    // Switch to table view to inspect row order.
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    const rows = screen.getAllByRole("row");
    // Row 0 is the header; rows 1+ are data rows.
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

    // Click the State column header to sort by state.
    fireEvent.click(screen.getByRole("columnheader", { name: /State/ }));

    const rows = screen.getAllByRole("row");
    // "running" < "stopped" alphabetically, so running first.
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

    // Default is name ascending: alpha first.
    let rows = screen.getAllByRole("row");
    expect(rows[1]).toHaveTextContent("alpha");
    expect(rows[2]).toHaveTextContent("zulu");

    // Click name header to toggle to descending.
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
    // Toggle to descending.
    fireEvent.click(screen.getByRole("columnheader", { name: /Name/ }));
    let rows = screen.getAllByRole("row");
    expect(rows[1]).toHaveTextContent("zulu");
    first.unmount();

    // Remount: sort preference is restored from localStorage.
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
    // The card view sort control shows the default "Name ▲".
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

    // Filter to running only.
    const runningChip = screen.getByRole("button", {
      name: t("dashboard.state.running"),
      pressed: false,
    });
    fireEvent.click(runningChip);

    // gamma-lobby (stopped) is filtered out.
    expect(screen.queryByText("gamma-lobby")).not.toBeInTheDocument();

    // Switch to table view to check sort order.
    fireEvent.click(
      screen.getByRole("button", { name: t("dashboard.view.table") }),
    );

    const rows = screen.getAllByRole("row");
    // Sorted by name ascending: alpha-creative before beta-survival.
    expect(rows[1]).toHaveTextContent("alpha-creative");
    expect(rows[2]).toHaveTextContent("beta-survival");
  });

  it("does not show filter controls when server list is empty", async () => {
    mockApi.get.mockResolvedValue([]);
    renderPage();

    await screen.findByText(t("dashboard.empty"));
    // The filter bar should not render when there are no servers at all.
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
    // Pass search and state filter via URL query params.
    renderPage(`/communities/${CID}?search=surv&state=running`);

    await screen.findByText("survival");
    // The search input should be pre-filled.
    const searchInput = screen.getByPlaceholderText(
      t("dashboard.filter.search"),
    );
    expect(searchInput).toHaveValue("surv");
    // "creative" (stopped) should be hidden by both the name and state filter.
    expect(screen.queryByText("creative")).not.toBeInTheDocument();
  });
});
