import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
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
    execution_backend: "container",
    game_port: 25565,
    desired_state: "running",
    observed_state: "running",
    observed_at: null,
    assigned_worker_id: "worker-a",
    config: {},
    ...overrides,
  };
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <DashboardPage />
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
    expect(screen.getByText("container")).toBeInTheDocument();
    expect(screen.getByText(":25565")).toBeInTheDocument();
    expect(screen.getByText("worker-a")).toBeInTheDocument();
    expect(screen.getByText(t("dashboard.state.running"))).toBeInTheDocument();
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
    mockApi.get.mockResolvedValue([server({ observed_state: "stopped" })]);
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
    mockApi.get.mockResolvedValue([server({ observed_state: "stopped" })]);
    mockApi.post.mockResolvedValue(server({ observed_state: "starting" }));
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: t("dashboard.start") }));

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/communities/${CID}/servers/s1/start`,
      ),
    );
    // The list refetches after the action settles.
    await waitFor(() => expect(mockApi.get).toHaveBeenCalledTimes(2));
  });

  it("routes a 403 through the permission glue, not a generic toast", async () => {
    mockApi.get.mockResolvedValue([server({ observed_state: "running" })]);
    mockApi.post.mockRejectedValue(
      new ApiError(403, { reason: "server:stop" }),
    );
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: t("dashboard.stop") }));

    expect(
      await screen.findByText(`${t("permissions.deniedNamed")}server:stop`),
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
    mockApi.get.mockResolvedValue([server({ observed_state: "stopped" })]);
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
    mockApi.get.mockResolvedValue([server({ observed_state: "stopped" })]);
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
});

describe("DashboardPage live status", () => {
  it("patches a card's pill from a status event without a refetch", async () => {
    mockApi.get.mockResolvedValue([server({ observed_state: "stopped" })]);
    renderPage();

    await screen.findByText("survival");
    expect(screen.getByText(t("dashboard.state.stopped"))).toBeInTheDocument();

    const socket = MockWebSocket.last();
    socket.open();
    socket.message({
      stream: "status",
      ts: "t",
      payload: { state: "running", detail: "" },
      server_id: "s1",
    });

    expect(
      await screen.findByText(t("dashboard.state.running")),
    ).toBeInTheDocument();
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
