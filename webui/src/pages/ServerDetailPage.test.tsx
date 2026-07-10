// @vitest-environment jsdom
// Pinned to jsdom: the copy-to-clipboard tests assert the
// document.execCommand("copy") fallback (skipped when happy-dom provides
// navigator.clipboard), and the WAI-ARIA focus-return test relies on jsdom's
// focus handling (issue #1751).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { setAccessToken } from "../auth/tokenStore.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { installMockWebSocket, MockWebSocket } from "../test/mockWebSocket.ts";
import { ServerDetailPage, sparklinePoints } from "./ServerDetailPage.tsx";

const CID = "c1";
const SID = "s1";

const mockApi = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));

vi.mock("../api/client.ts", async () => {
  const actual =
    await vi.importActual<typeof import("../api/client.ts")>(
      "../api/client.ts",
    );
  return { ...actual, api: mockApi };
});

const mockDownload = vi.hoisted(() => ({ downloadFile: vi.fn() }));
vi.mock("../api/download.ts", () => mockDownload);

let mockCan: Can = () => true;
vi.mock("../permissions/ActiveCommunityProvider.tsx", () => ({
  useActiveCommunity: () => ({
    communityId: CID,
    setCommunityId: vi.fn(),
    communities: [{ id: CID, name: "Sakura" }],
  }),
}));
vi.mock("../permissions/useCan.ts", () => ({ useCan: () => mockCan }));

// Spy on navigate while keeping the real one working: tab switches now drive the
// URL hash (#514), so the navigate must actually update the location, not no-op.
// The spy records the calls (the delete test asserts the dashboard redirect).
const navigateMock = vi.hoisted(() => vi.fn());
vi.mock("react-router", async () => {
  const actual =
    await vi.importActual<typeof import("react-router")>("react-router");
  return {
    ...actual,
    useNavigate: () => {
      const real = actual.useNavigate();
      return (...args: Parameters<typeof real>) => {
        navigateMock(...args);
        return real(...args);
      };
    },
  };
});

function server(overrides: Record<string, unknown> = {}) {
  return {
    id: SID,
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
    slug: "survival",
    join_hostname: null,
    bedrock_address: null,
    bedrock_port: null,
    ...overrides,
  };
}

// A history probe: drives navigate(-1) so a test can simulate the Back button
// against the in-memory router (#514 tab history).
function BackProbe() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(-1)}>
      router-back
    </button>
  );
}

function renderPage(path = `/communities/${CID}/servers/${SID}`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <BackProbe />
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
  return { ...result, queryClient };
}

beforeEach(() => {
  setAccessToken("tok-1");
  mockApi.get.mockReset();
  mockApi.post.mockReset();
  mockApi.patch.mockReset();
  mockApi.delete.mockReset();
  mockDownload.downloadFile.mockReset();
  navigateMock.mockReset();
  mockCan = () => true;
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("ServerDetailPage scaffold + header", () => {
  it("renders the header with name, state pill, worker, port and tabs", async () => {
    mockApi.get.mockResolvedValue(server());
    renderPage();

    expect(await screen.findByText("survival")).toBeInTheDocument();
    // The state pill carries the observed-state label with the pill class.
    const pill = screen
      .getAllByText(t("dashboard.state.running"))
      .find((el) => el.className.includes("pill"));
    expect(pill).toBeDefined();
    // The worker chip is labelled and the id abbreviated (#644): "worker-a"
    // shortens to its leading segment.
    expect(
      screen.getByText(`${t("serverDetail.worker")}: worker`),
    ).toBeInTheDocument();
    expect(screen.getByText(":25565")).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: t("serverDetail.tab.overview") }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: t("serverDetail.tab.settings") }),
    ).toBeInTheDocument();
  });

  it("names the h1 by the server alone, not the status pill (a11y; #647)", async () => {
    mockApi.get.mockResolvedValue(server());
    renderPage();

    const heading = await screen.findByRole("heading", { level: 1 });
    expect(heading).toHaveAccessibleName("survival");
    expect(heading).not.toHaveAccessibleName(/running/i);
  });

  it("carries the full worker id in a hover title (#644)", async () => {
    const fullId = "ad1051a7-1234-5678-9abc-def012345678";
    mockApi.get.mockResolvedValue(server({ assigned_worker_id: fullId }));
    renderPage();

    const chip = await screen.findByText(
      `${t("serverDetail.worker")}: ad1051a7`,
    );
    expect(chip).toHaveAttribute("title", fullId);
  });

  it("labels the stopped-server chip as no worker assigned (#644)", async () => {
    mockApi.get.mockResolvedValue(
      server({ assigned_worker_id: null, observed_state: "stopped" }),
    );
    renderPage();

    expect(
      await screen.findByText(t("serverDetail.noWorker")),
    ).toBeInTheDocument();
  });

  it("shows the converging hint when desired ≠ observed", async () => {
    mockApi.get.mockResolvedValue(
      server({ desired_state: "running", observed_state: "starting" }),
    );
    renderPage();

    expect(
      await screen.findByText(t("serverDetail.converging")),
    ).toBeInTheDocument();
  });

  it("surfaces a load error", async () => {
    mockApi.get.mockRejectedValue(new ApiError(500, {}));
    renderPage();

    expect(
      await screen.findByText(t("serverDetail.loadError")),
    ).toBeInTheDocument();
  });

  it("keeps rendering cached data when a background refetch fails (#1724)", async () => {
    mockApi.get.mockResolvedValue(server());
    const { queryClient } = renderPage();
    await screen.findByText("survival");

    // Simulate a transient API outage: the next background refetch fails.
    mockApi.get.mockRejectedValue(new ApiError(500, {}));
    await act(() => queryClient.invalidateQueries());
    // The query-state notification lands a task after invalidateQueries
    // settles; flush it so the assertion sees the post-refetch render.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // The cached page stays on screen instead of a full-page error.
    expect(screen.getByText("survival")).toBeInTheDocument();
    expect(
      screen.queryByText(t("serverDetail.loadError")),
    ).not.toBeInTheDocument();
  });

  it("recovers to fresh data once a refetch succeeds after a failure (#1724)", async () => {
    mockApi.get.mockResolvedValue(server());
    const { queryClient } = renderPage();
    await screen.findByText("survival");

    mockApi.get.mockRejectedValue(new ApiError(500, {}));
    await act(() => queryClient.invalidateQueries());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // The API comes back: the next refetch replaces the stale data.
    mockApi.get.mockResolvedValue(server({ name: "renamed" }));
    await act(() => queryClient.invalidateQueries());

    expect(await screen.findByText("renamed")).toBeInTheDocument();
    expect(
      screen.queryByText(t("serverDetail.loadError")),
    ).not.toBeInTheDocument();
  });
});

describe("ServerDetailPage loader-aware content tab (#1320)", () => {
  it("labels the content tab 'Plugins' for a paper server", async () => {
    mockApi.get.mockResolvedValue(server({ server_type: "paper" }));
    renderPage();

    expect(
      await screen.findByRole("tab", { name: t("serverDetail.tab.plugins") }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("tab", { name: t("serverDetail.tab.mods") }),
    ).not.toBeInTheDocument();
  });

  it("labels the content tab 'Mods' for fabric and forge servers", async () => {
    for (const type of ["fabric", "forge"]) {
      mockApi.get.mockResolvedValue(server({ server_type: type }));
      const { unmount } = renderPage();

      expect(
        await screen.findByRole("tab", { name: t("serverDetail.tab.mods") }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("tab", { name: t("serverDetail.tab.plugins") }),
      ).not.toBeInTheDocument();
      unmount();
    }
  });

  it("hides the content tab for vanilla servers", async () => {
    mockApi.get.mockResolvedValue(server({ server_type: "vanilla" }));
    renderPage();

    await screen.findByRole("tab", { name: t("serverDetail.tab.overview") });
    expect(
      screen.queryByRole("tab", { name: t("serverDetail.tab.plugins") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("tab", { name: t("serverDetail.tab.mods") }),
    ).not.toBeInTheDocument();
  });
});

describe("ServerDetailPage URL-driven tabs (#514)", () => {
  let restoreWs: () => void;
  beforeEach(() => {
    restoreWs = installMockWebSocket();
  });
  afterEach(() => restoreWs());

  function activeTab(): string | null {
    return (
      screen
        .getAllByRole("tab")
        .find((el) => el.getAttribute("aria-selected") === "true")
        ?.textContent ?? null
    );
  }

  it("deep-links to a tab named by the URL hash", async () => {
    mockApi.get.mockResolvedValue(server());
    renderPage(`/communities/${CID}/servers/${SID}#settings`);

    await screen.findByText("survival");
    expect(activeTab()).toBe(t("serverDetail.tab.settings"));
    expect(screen.getByDisplayValue("survival")).toBeInTheDocument();
  });

  it("Back restores the previously active tab", async () => {
    mockApi.get.mockResolvedValue(server());
    renderPage();
    await screen.findByText("survival");
    expect(activeTab()).toBe(t("serverDetail.tab.overview"));

    fireEvent.click(
      screen.getByRole("tab", { name: t("serverDetail.tab.settings") }),
    );
    expect(activeTab()).toBe(t("serverDetail.tab.settings"));

    // Simulate the browser Back button: navigate(-1) pops the pushed tab entry.
    fireEvent.click(screen.getByText("router-back"));
    await waitFor(() =>
      expect(activeTab()).toBe(t("serverDetail.tab.overview")),
    );
  });

  it("tab buttons carry aria-controls and the panel carries aria-labelledby (#1216)", async () => {
    mockApi.get.mockResolvedValue(server());
    renderPage();
    await screen.findByText("survival");

    const overviewTab = screen.getByRole("tab", {
      name: t("serverDetail.tab.overview"),
    });
    expect(overviewTab).toHaveAttribute("aria-controls", "sd-panel-overview");
    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveAttribute("id", "sd-panel-overview");
    expect(panel).toHaveAttribute("aria-labelledby", "sd-tab-overview");
  });

  it("ArrowRight moves focus to the next tab (#1216)", async () => {
    mockApi.get.mockResolvedValue(server());
    renderPage();
    await screen.findByText("survival");

    const overviewTab = screen.getByRole("tab", {
      name: t("serverDetail.tab.overview"),
    });
    overviewTab.focus();
    fireEvent.keyDown(overviewTab, { key: "ArrowRight" });

    const consoleTab = screen.getByRole("tab", {
      name: t("serverDetail.tab.console"),
    });
    expect(consoleTab).toHaveFocus();
    expect(consoleTab).toHaveAttribute("aria-selected", "true");
  });

  it("inactive tabs have tabIndex -1 (roving tabindex, #1216)", async () => {
    mockApi.get.mockResolvedValue(server());
    renderPage();
    await screen.findByText("survival");

    const overviewTab = screen.getByRole("tab", {
      name: t("serverDetail.tab.overview"),
    });
    const settingsTab = screen.getByRole("tab", {
      name: t("serverDetail.tab.settings"),
    });
    expect(overviewTab).toHaveAttribute("tabindex", "0");
    expect(settingsTab).toHaveAttribute("tabindex", "-1");
  });
});

describe("ServerDetailPage lifecycle controls", () => {
  it("gates controls by state machine: running shows stop + restart, hides start", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();

    await screen.findByText("survival");
    expect(screen.getByRole("button", { name: /Stop/ })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: t("serverDetail.restart") }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("serverDetail.start") }),
    ).not.toBeInTheDocument();
  });

  it("shows start (only) on a stopped server", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", desired_state: "stopped" }),
    );
    renderPage();

    await screen.findByText("survival");
    expect(
      screen.getByRole("button", { name: t("serverDetail.start") }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Stop/ }),
    ).not.toBeInTheDocument();
  });

  it("hides a control the caller may not perform", async () => {
    mockCan = (code) => code !== "server:restart";
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();

    await screen.findByText("survival");
    expect(
      screen.queryByRole("button", { name: t("serverDetail.restart") }),
    ).not.toBeInTheDocument();
  });

  it("force-stops via the dropdown with ?force=true after confirmation", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    mockApi.post.mockResolvedValue(server({ observed_state: "stopping" }));
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: /Stop/ }));
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("serverDetail.stopForce") }),
    );

    // The confirmation dialog should appear.
    expect(
      screen.getByText(t("serverDetail.forceStop.dialogBody")),
    ).toBeInTheDocument();
    // The API must NOT have been called yet.
    expect(mockApi.post).not.toHaveBeenCalled();

    // Confirm the force stop.
    fireEvent.click(
      screen.getByRole("button", {
        name: t("serverDetail.forceStop.confirm"),
      }),
    );

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/stop?force=true`,
      ),
    );
  });

  it("cancelling the force-stop confirmation does not send the request", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: /Stop/ }));
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("serverDetail.stopForce") }),
    );

    // Dialog is open.
    expect(
      screen.getByText(t("serverDetail.forceStop.dialogBody")),
    ).toBeInTheDocument();

    // Cancel.
    fireEvent.click(screen.getByRole("button", { name: t("common.cancel") }));

    // Dialog closes, no API call.
    expect(
      screen.queryByText(t("serverDetail.forceStop.dialogBody")),
    ).not.toBeInTheDocument();
    expect(mockApi.post).not.toHaveBeenCalled();
  });

  it("graceful-stops via the dropdown without the force flag", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    mockApi.post.mockResolvedValue(server({ observed_state: "stopping" }));
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: /Stop/ }));
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("serverDetail.stopGraceful") }),
    );

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/stop`,
      ),
    );
  });

  it("closes the stop menu on a click outside it", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: /Stop/ }));
    expect(
      screen.getByRole("menuitem", { name: t("serverDetail.stopForce") }),
    ).toBeInTheDocument();

    fireEvent.click(document.body);
    expect(
      screen.queryByRole("menuitem", { name: t("serverDetail.stopForce") }),
    ).not.toBeInTheDocument();
  });

  it("closes the stop menu on Escape", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: /Stop/ }));
    expect(
      screen.getByRole("menuitem", { name: t("serverDetail.stopForce") }),
    ).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(
      screen.queryByRole("menuitem", { name: t("serverDetail.stopForce") }),
    ).not.toBeInTheDocument();
  });

  describe("stop menu WAI-ARIA keyboard pattern (#496)", () => {
    function items() {
      return screen.getAllByRole("menuitem");
    }
    async function openWith(key: string) {
      mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
      mockApi.post.mockResolvedValue(server({ observed_state: "stopping" }));
      renderPage();
      await screen.findByText("survival");
      const trigger = screen.getByRole("button", { name: /Stop/ });
      trigger.focus();
      fireEvent.keyDown(trigger, { key });
      return trigger;
    }

    it("opening with Enter focuses the first item with roving tabindex", async () => {
      await openWith("Enter");
      const [graceful, force] = items();
      expect(graceful).toHaveFocus();
      expect(graceful).toHaveAttribute("tabindex", "0");
      expect(force).toHaveAttribute("tabindex", "-1");
    });

    it("opening with ArrowDown focuses the first item", async () => {
      await openWith("ArrowDown");
      expect(items()[0]).toHaveFocus();
    });

    it("opening with ArrowUp focuses the last item", async () => {
      await openWith("ArrowUp");
      const list = items();
      expect(list[list.length - 1]).toHaveFocus();
    });

    it("ArrowDown / ArrowUp move focus and wrap with roving tabindex", async () => {
      await openWith("Enter");
      const [graceful, force] = items();

      fireEvent.keyDown(graceful, { key: "ArrowDown" });
      expect(force).toHaveFocus();
      expect(force).toHaveAttribute("tabindex", "0");
      expect(graceful).toHaveAttribute("tabindex", "-1");

      // Wrap forward past the last item back to the first.
      fireEvent.keyDown(force, { key: "ArrowDown" });
      expect(graceful).toHaveFocus();

      // Wrap backward from the first to the last.
      fireEvent.keyDown(graceful, { key: "ArrowUp" });
      expect(force).toHaveFocus();
    });

    it("Home / End jump to the first / last item", async () => {
      await openWith("Enter");
      const [graceful, force] = items();

      fireEvent.keyDown(graceful, { key: "End" });
      expect(force).toHaveFocus();

      fireEvent.keyDown(force, { key: "Home" });
      expect(graceful).toHaveFocus();
    });

    it("type-ahead moves focus to the next item starting with the typed key", async () => {
      await openWith("Enter");
      const [graceful, force] = items();

      // "Force stop" starts with F.
      fireEvent.keyDown(graceful, { key: "f" });
      expect(force).toHaveFocus();

      // "Stop (graceful)" starts with S.
      fireEvent.keyDown(force, { key: "s" });
      expect(graceful).toHaveFocus();
    });

    it("Enter activates the focused item (force requires confirmation)", async () => {
      await openWith("ArrowUp"); // focuses Force stop
      fireEvent.keyDown(items()[1], { key: "Enter" });
      // Force stop opens the confirmation dialog instead of firing immediately.
      expect(
        screen.getByText(t("serverDetail.forceStop.dialogBody")),
      ).toBeInTheDocument();
      fireEvent.click(
        screen.getByRole("button", {
          name: t("serverDetail.forceStop.confirm"),
        }),
      );
      await waitFor(() =>
        expect(mockApi.post).toHaveBeenCalledWith(
          `/api/communities/${CID}/servers/${SID}/stop?force=true`,
        ),
      );
    });

    it("Space activates the focused item", async () => {
      await openWith("Enter"); // focuses Stop (graceful)
      fireEvent.keyDown(items()[0], { key: " " });
      await waitFor(() =>
        expect(mockApi.post).toHaveBeenCalledWith(
          `/api/communities/${CID}/servers/${SID}/stop`,
        ),
      );
    });

    it("Escape closes the menu and returns focus to the trigger", async () => {
      const trigger = await openWith("Enter");
      expect(items()[0]).toHaveFocus();

      fireEvent.keyDown(items()[0], { key: "Escape" });
      expect(
        screen.queryByRole("menuitem", { name: t("serverDetail.stopForce") }),
      ).not.toBeInTheDocument();
      expect(trigger).toHaveFocus();
    });

    it("returns focus to the trigger after keyboard activation of graceful (APG)", async () => {
      const trigger = await openWith("Enter");
      fireEvent.keyDown(items()[0], { key: "Enter" });
      expect(
        screen.queryByRole("menuitem", { name: t("serverDetail.stopForce") }),
      ).not.toBeInTheDocument();
      expect(trigger).toHaveFocus();
    });

    it("returns focus to the trigger after click activation of force via confirm (APG)", async () => {
      const trigger = await openWith("Enter");
      fireEvent.click(items()[1]);
      // Force opens the confirm dialog; the menu closes.
      expect(
        screen.queryByRole("menuitem", { name: t("serverDetail.stopForce") }),
      ).not.toBeInTheDocument();
      // Confirm the force stop; focus returns to the trigger.
      fireEvent.click(
        screen.getByRole("button", {
          name: t("serverDetail.forceStop.confirm"),
        }),
      );
      expect(trigger).toHaveFocus();
    });
  });

  it("routes a lifecycle 403 through the permission glue", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    mockApi.post.mockRejectedValue(
      new ApiError(403, { reason: "forbidden", permission: "server:restart" }),
    );
    renderPage();

    await screen.findByText("survival");
    // restart is permitted by the resolver but the server denies at run-time.
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.restart") }),
    );

    expect(
      await screen.findByText(
        t("permissions.deniedNamed", { permission: "server:restart" }),
      ),
    ).toBeInTheDocument();
  });

  it("gives a lifecycle 409 the state-changed treatment", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "server_unsettled" }),
    );
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.restart") }),
    );

    expect(
      await screen.findByText(t("dashboard.stateChanged")),
    ).toBeInTheDocument();
  });

  describe("optimistic state transition (#1071)", () => {
    let restoreWs: () => void;
    beforeEach(() => {
      restoreWs = installMockWebSocket();
    });
    afterEach(() => restoreWs());

    function statePill(): string | null {
      const pills = document.querySelectorAll(".detail-title .pill");
      return pills[0]?.textContent ?? null;
    }

    it("optimistically shows the starting pill immediately on start", async () => {
      mockApi.get.mockResolvedValue(
        server({ observed_state: "stopped", desired_state: "stopped" }),
      );
      mockApi.post.mockReturnValue(new Promise(() => {}));
      renderPage();

      await screen.findByText("survival");
      fireEvent.click(
        screen.getByRole("button", { name: t("serverDetail.start") }),
      );

      await waitFor(() =>
        expect(statePill()).toBe(t("dashboard.state.starting")),
      );
    });

    it("optimistically shows the stopping pill on stop", async () => {
      mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
      mockApi.post.mockReturnValue(new Promise(() => {}));
      renderPage();

      await screen.findByText("survival");
      fireEvent.click(screen.getByRole("button", { name: /Stop/ }));
      fireEvent.click(
        screen.getByRole("menuitem", { name: t("serverDetail.stopGraceful") }),
      );

      await waitFor(() =>
        expect(statePill()).toBe(t("dashboard.state.stopping")),
      );
    });

    it("reverts the pill to the previous state on lifecycle error", async () => {
      mockApi.get.mockResolvedValue(
        server({ observed_state: "stopped", desired_state: "stopped" }),
      );
      mockApi.post.mockRejectedValue(
        new ApiError(409, { reason: "port_conflict" }),
      );
      renderPage();

      await screen.findByText("survival");
      fireEvent.click(
        screen.getByRole("button", { name: t("serverDetail.start") }),
      );

      // After the error, the pill reverts to "Stopped".
      await waitFor(() =>
        expect(statePill()).toBe(t("dashboard.state.stopped")),
      );
    });
  });
});

describe("ServerDetailPage export", () => {
  it("downloads the export ZIP through the authenticated helper", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", desired_state: "stopped" }),
    );
    mockDownload.downloadFile.mockResolvedValue(undefined);
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.export") }),
    );

    await waitFor(() =>
      expect(mockDownload.downloadFile).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/export`,
        "survival.zip",
      ),
    );
  });

  it("disables export while the server is running", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();

    await screen.findByText("survival");
    expect(
      screen.getByRole("button", { name: t("serverDetail.export") }),
    ).toBeDisabled();
  });

  it("hides export when the caller lacks file:read", async () => {
    mockCan = (code) => code !== "file:read";
    mockApi.get.mockResolvedValue(server({ observed_state: "stopped" }));
    renderPage();

    await screen.findByText("survival");
    expect(
      screen.queryByRole("button", { name: t("serverDetail.export") }),
    ).not.toBeInTheDocument();
  });
});

describe("ServerDetailPage settings", () => {
  // Install the mock socket so the events client stays "connected" and never
  // fires onDown -> invalidate, which would refetch the detail query and
  // consume the port-check mockResolvedValueOnce out from under the test.
  let restoreWs: () => void;

  beforeEach(() => {
    restoreWs = installMockWebSocket();
  });
  afterEach(() => {
    restoreWs();
  });

  function openSettings() {
    fireEvent.click(
      screen.getByRole("tab", { name: t("serverDetail.tab.settings") }),
    );
  }

  it("renders name, port, read-only backend and config rows from the server", async () => {
    mockApi.get.mockResolvedValue(
      server({ config: { "max-players": "20", difficulty: "hard" } }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    expect(screen.getByDisplayValue("survival")).toBeInTheDocument();
    expect(screen.getByDisplayValue("25565")).toBeInTheDocument();
    expect(screen.getByDisplayValue("max-players")).toBeInTheDocument();
    expect(screen.getByDisplayValue("hard")).toBeInTheDocument();
  });

  it("hides the game-port control in relay mode (#1002)", async () => {
    // A non-null join_hostname signals relay mode: players join port-less, so
    // the port is internal plumbing the API manages and the control is hidden.
    mockApi.get.mockResolvedValue(
      server({ join_hostname: "survival.mc.example.com" }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    expect(
      screen.queryByLabelText(t("serverDetail.settings.gamePort")),
    ).toBeNull();
    // The slug (join address name) control is shown in relay mode.
    expect(
      screen.getByLabelText(t("serverDetail.settings.slug")),
    ).toBeInTheDocument();
  });

  it("shows the game-port control in direct mode (relay off, #1002)", async () => {
    mockApi.get.mockResolvedValue(server({ join_hostname: null }));
    renderPage();

    await screen.findByText("survival");
    openSettings();

    expect(
      screen.getByLabelText(t("serverDetail.settings.gamePort")),
    ).toBeInTheDocument();
  });

  it("omits game_port from the PATCH body in relay mode (#1002)", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        join_hostname: "survival.mc.example.com",
        config: { motd: "hi" },
      }),
    );
    mockApi.patch.mockResolvedValue(server());
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const [, init] = mockApi.patch.mock.calls[0];
    expect(JSON.parse(init.body).game_port).toBeUndefined();
  });

  it("checks port availability on blur and shows the taken hint", async () => {
    // Route by path so that the resource-pack assignment query (#1179) and the
    // meta query don't consume the port-check response.
    const srv = server({ observed_state: "stopped" });
    mockApi.get.mockImplementation((path: string) => {
      if (path.endsWith("/resource-pack")) {
        return Promise.reject(new ApiError(404, { reason: "not_found" }));
      }
      if (path === "/api/meta") {
        return Promise.resolve({
          relay_enabled: false,
          default_memory_limit_mb: null,
          max_memory_limit_mb: null,
        });
      }
      if (path.startsWith("/api/ports/check/")) {
        return Promise.resolve({
          port: 25570,
          in_range: true,
          available: false,
        });
      }
      return Promise.resolve(srv);
    });
    renderPage();

    await screen.findByText("survival");
    openSettings();
    const portInput = screen.getByDisplayValue("25565");
    fireEvent.change(portInput, { target: { value: "25570" } });
    fireEvent.blur(portInput);

    await waitFor(() =>
      expect(mockApi.get).toHaveBeenCalledWith("/api/ports/check/25570"),
    );
    expect(
      await screen.findByText(t("serverDetail.port.taken")),
    ).toBeInTheDocument();
  });

  it("ignores a stale out-of-order port-check response (#1592)", async () => {
    // Two blurs in quick succession on different ports: the first port's check
    // resolves *after* the second's. Without a request guard the stale "taken"
    // verdict for 25570 would clobber the current "available" verdict for 25571.
    const srv = server({ observed_state: "stopped" });
    let resolveFirst: (v: unknown) => void = () => {};
    let resolveSecond: (v: unknown) => void = () => {};
    mockApi.get.mockImplementation((path: string) => {
      if (path.endsWith("/resource-pack")) {
        return Promise.reject(new ApiError(404, { reason: "not_found" }));
      }
      if (path === "/api/meta") {
        return Promise.resolve({
          relay_enabled: false,
          default_memory_limit_mb: null,
          max_memory_limit_mb: null,
        });
      }
      if (path === "/api/ports/check/25570") {
        return new Promise((resolve) => {
          resolveFirst = resolve;
        });
      }
      if (path === "/api/ports/check/25571") {
        return new Promise((resolve) => {
          resolveSecond = resolve;
        });
      }
      return Promise.resolve(srv);
    });
    renderPage();

    await screen.findByText("survival");
    openSettings();
    const portInput = screen.getByDisplayValue("25565");
    // First blur on 25570 leaves its check pending.
    fireEvent.change(portInput, { target: { value: "25570" } });
    fireEvent.blur(portInput);
    // Second blur on 25571 supersedes it.
    fireEvent.change(portInput, { target: { value: "25571" } });
    fireEvent.blur(portInput);

    // The current (second) request resolves first: available.
    resolveSecond({ port: 25571, in_range: true, available: true });
    expect(
      await screen.findByText(t("serverDetail.port.available")),
    ).toBeInTheDocument();

    // The stale (first) request resolves later as taken — it must be ignored.
    resolveFirst({ port: 25570, in_range: true, available: false });
    await waitFor(() =>
      expect(
        screen.getByText(t("serverDetail.port.available")),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText(t("serverDetail.port.taken"))).toBeNull();
  });

  it("saves name + port + config round-trip via PATCH", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", config: { motd: "hi" } }),
    );
    mockApi.patch.mockResolvedValue(server());
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(screen.getByDisplayValue("survival"), {
      target: { value: "renamed" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const [path, init] = mockApi.patch.mock.calls[0];
    expect(path).toBe(`/api/communities/${CID}/servers/${SID}`);
    expect(JSON.parse(init.body)).toEqual({
      name: "renamed",
      game_port: 25565,
      config: { motd: "hi" },
    });
  });

  it("adds and removes config rows", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", config: {} }),
    );
    mockApi.patch.mockResolvedValue(server());
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.click(
      screen.getByRole("button", {
        name: t("serverDetail.settings.configAdd"),
      }),
    );
    const keyInputs = screen.getAllByLabelText(
      t("serverDetail.settings.configKey"),
    );
    fireEvent.change(keyInputs[keyInputs.length - 1], {
      target: { value: "view-distance" },
    });
    const valueInputs = screen.getAllByLabelText(
      t("serverDetail.settings.configValue"),
    );
    fireEvent.change(valueInputs[valueInputs.length - 1], {
      target: { value: "10" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    // A freshly typed `10` is parsed with JSON-value semantics → a number.
    const config = JSON.parse(mockApi.patch.mock.calls[0][1].body).config;
    expect(config).toEqual({ "view-distance": 10 });
    expect(typeof config["view-distance"]).toBe("number");
  });

  it("preserves an untouched non-string config value when another key is renamed", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        config: { snapshot_interval_seconds: 3600, motd: "hi" },
      }),
    );
    mockApi.patch.mockResolvedValue(server());
    renderPage();

    await screen.findByText("survival");
    openSettings();
    // Rename only the `motd` key; the integer override row is untouched.
    fireEvent.change(screen.getByDisplayValue("motd"), {
      target: { value: "greeting" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const config = JSON.parse(mockApi.patch.mock.calls[0][1].body).config;
    expect(config).toEqual({ snapshot_interval_seconds: 3600, greeting: "hi" });
    // The untouched integer survives as a number, not "3600".
    expect(typeof config.snapshot_interval_seconds).toBe("number");
  });

  it("preserves the original value type when only a row's key is renamed (#791)", async () => {
    // A string value that parses as a JSON literal ("12" → 12 via
    // parseConfigValue) must stay a string if the user only renamed the key —
    // the row must NOT be flagged as edited in that case.
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        config: { motd: "12" },
      }),
    );
    mockApi.patch.mockResolvedValue(server());
    renderPage();

    await screen.findByText("survival");
    openSettings();
    // Rename the key — the value "12" is untouched.
    fireEvent.change(screen.getByDisplayValue("motd"), {
      target: { value: "greeting" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const config = JSON.parse(mockApi.patch.mock.calls[0][1].body).config;
    // "12" must stay a string — the key rename must not trigger re-parsing.
    expect(config).toEqual({ greeting: "12" });
    expect(typeof config.greeting).toBe("string");
  });

  it("sends an edited integer config value as a number", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        config: { snapshot_interval_seconds: 3600 },
      }),
    );
    mockApi.patch.mockResolvedValue(server());
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(screen.getByDisplayValue("3600"), {
      target: { value: "7200" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const config = JSON.parse(mockApi.patch.mock.calls[0][1].body).config;
    expect(config).toEqual({ snapshot_interval_seconds: 7200 });
    expect(typeof config.snapshot_interval_seconds).toBe("number");
  });

  it("hides the system-managed resolved_jar_sha256 key from the overrides editor", async () => {
    mockApi.get.mockResolvedValue(
      server({
        config: { resolved_jar_sha256: "abc123", motd: "hi" },
      }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    expect(screen.getByDisplayValue("motd")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("resolved_jar_sha256")).toBeNull();
    expect(screen.queryByDisplayValue("abc123")).toBeNull();
  });

  it("preserves the hidden resolved_jar_sha256 key on save", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        config: { resolved_jar_sha256: "abc123", motd: "hi" },
      }),
    );
    mockApi.patch.mockResolvedValue(server());
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(screen.getByDisplayValue("hi"), {
      target: { value: "bye" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const config = JSON.parse(mockApi.patch.mock.calls[0][1].body).config;
    // The user only edited `motd`, but the system-managed key round-trips
    // untouched rather than being dropped from the replaced config blob.
    expect(config).toEqual({ resolved_jar_sha256: "abc123", motd: "bye" });
  });

  it("surfaces a 422 invalid_snapshot_interval specifically on save", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "stopped" }));
    mockApi.patch.mockRejectedValue(
      new ApiError(422, { reason: "invalid_snapshot_interval" }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    expect(
      await screen.findByText(t("serverDetail.error.invalidSnapshotInterval")),
    ).toBeInTheDocument();
  });

  it("surfaces a 422 invalid_backup_schedule specifically on save", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "stopped" }));
    mockApi.patch.mockRejectedValue(
      new ApiError(422, { reason: "invalid_backup_schedule" }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    expect(
      await screen.findByText(t("serverDetail.error.invalidBackupSchedule")),
    ).toBeInTheDocument();
  });

  it("surfaces a 409 server_not_stopped specifically on save", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    mockApi.patch.mockRejectedValue(
      new ApiError(409, { reason: "server_not_stopped" }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    expect(
      await screen.findByText(t("serverDetail.error.notStopped")),
    ).toBeInTheDocument();
  });

  it("disables the save button without server:update", async () => {
    mockCan = (code) => code !== "server:update";
    mockApi.get.mockResolvedValue(server({ observed_state: "stopped" }));
    renderPage();

    await screen.findByText("survival");
    openSettings();
    expect(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    ).toBeDisabled();
  });

  it("re-syncs form fields when the server prop changes (#1212)", async () => {
    mockApi.get.mockResolvedValue(
      server({
        name: "old-name",
        slug: "old-slug",
        game_port: 25565,
        memory_limit_mb: 1024,
        cpu_millis: 1000,
        config: { motd: "hello" },
        observed_state: "stopped",
      }),
    );
    const { queryClient } = renderPage();

    await screen.findByText("old-name");
    openSettings();
    expect(screen.getByDisplayValue("old-name")).toBeInTheDocument();
    expect(screen.getByDisplayValue("25565")).toBeInTheDocument();
    expect(screen.getByDisplayValue("1024")).toBeInTheDocument();
    expect(screen.getByDisplayValue("1000")).toBeInTheDocument();

    // Simulate a react-query refetch returning updated server data.
    mockApi.get.mockResolvedValue(
      server({
        name: "new-name",
        slug: "new-slug",
        game_port: 25570,
        memory_limit_mb: 2048,
        cpu_millis: 1500,
        config: { motd: "bye" },
        observed_state: "stopped",
      }),
    );
    await act(() => queryClient.invalidateQueries());

    await waitFor(() =>
      expect(screen.getByDisplayValue("new-name")).toBeInTheDocument(),
    );
    expect(screen.getByDisplayValue("25570")).toBeInTheDocument();
    expect(screen.getByDisplayValue("2048")).toBeInTheDocument();
    expect(screen.getByDisplayValue("1500")).toBeInTheDocument();
    expect(screen.getByDisplayValue("bye")).toBeInTheDocument();
  });
});

describe("ServerDetailPage settings memory limit", () => {
  let restoreWs: () => void;

  beforeEach(() => {
    restoreWs = installMockWebSocket();
  });
  afterEach(() => {
    restoreWs();
  });

  function openSettings() {
    fireEvent.click(
      screen.getByRole("tab", { name: t("serverDetail.tab.settings") }),
    );
  }

  function memoryInput() {
    return screen.getByLabelText(t("serverDetail.settings.memoryLimit"));
  }

  it("shows the driver-default placeholder when the limit is unset", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", memory_limit_mb: null, config: {} }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    const input = memoryInput() as HTMLInputElement;
    expect(input.value).toBe("");
    expect(input).toHaveAttribute(
      "placeholder",
      t("serverDetail.settings.memoryLimitDefault"),
    );
  });

  it("shows the current limit when set", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        memory_limit_mb: 2048,
        config: { memory_limit_mb: 2048 },
      }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    expect((memoryInput() as HTMLInputElement).value).toBe("2048");
  });

  it("does not show memory_limit_mb as a raw config override row", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        memory_limit_mb: 2048,
        config: { memory_limit_mb: 2048, motd: "hi" },
      }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    expect(screen.getByDisplayValue("motd")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("memory_limit_mb")).toBeNull();
  });

  it("saves a set memory limit into the config blob", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", memory_limit_mb: null, config: {} }),
    );
    mockApi.patch.mockResolvedValue(server());
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(memoryInput(), { target: { value: "2048" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const config = JSON.parse(mockApi.patch.mock.calls[0][1].body).config;
    expect(config).toEqual({ memory_limit_mb: 2048 });
    expect(typeof config.memory_limit_mb).toBe("number");
  });

  it("omits the key from the config blob when the limit is cleared", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        memory_limit_mb: 2048,
        config: { memory_limit_mb: 2048, motd: "hi" },
      }),
    );
    mockApi.patch.mockResolvedValue(server());
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(memoryInput(), { target: { value: "" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const config = JSON.parse(mockApi.patch.mock.calls[0][1].body).config;
    expect(config).toEqual({ motd: "hi" });
  });

  it("rejects a below-floor value and blocks the save", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", memory_limit_mb: null, config: {} }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(memoryInput(), { target: { value: "256" } });

    expect(
      await screen.findByText(t("serverDetail.settings.memoryLimitRange")),
    ).toBeInTheDocument();
    expect(mockApi.patch).not.toHaveBeenCalled();
  });

  it("rejects an above-ceiling value and blocks the save", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", memory_limit_mb: null, config: {} }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(memoryInput(), { target: { value: "1048577" } });

    expect(
      await screen.findByText(t("serverDetail.settings.memoryLimitRange")),
    ).toBeInTheDocument();
    expect(mockApi.patch).not.toHaveBeenCalled();
  });

  it("rejects a non-integer value and blocks the save", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", memory_limit_mb: null, config: {} }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(memoryInput(), { target: { value: "1024.5" } });

    expect(
      await screen.findByText(t("serverDetail.settings.memoryLimitRange")),
    ).toBeInTheDocument();
    expect(mockApi.patch).not.toHaveBeenCalled();
  });

  it("disables the memory limit field without server:update", async () => {
    mockCan = (code) => code !== "server:update";
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", memory_limit_mb: 2048 }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();
    expect(memoryInput()).toBeDisabled();
  });

  it("surfaces a 422 invalid_memory_limit specifically on save", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", memory_limit_mb: null, config: {} }),
    );
    mockApi.patch.mockRejectedValue(
      new ApiError(422, { reason: "invalid_memory_limit" }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(memoryInput(), { target: { value: "2048" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    expect(
      await screen.findByText(t("serverDetail.error.invalidMemoryLimit")),
    ).toBeInTheDocument();
  });
});

describe("ServerDetailPage settings CPU allocation", () => {
  let restoreWs: () => void;

  beforeEach(() => {
    restoreWs = installMockWebSocket();
  });
  afterEach(() => {
    restoreWs();
  });

  function openSettings() {
    fireEvent.click(
      screen.getByRole("tab", { name: t("serverDetail.tab.settings") }),
    );
  }

  function cpuInput() {
    return screen.getByLabelText(t("serverDetail.settings.cpuAllocation"));
  }

  it("shows the auto placeholder when the allocation is unset", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", cpu_millis: null, config: {} }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    const input = cpuInput() as HTMLInputElement;
    expect(input.value).toBe("");
    expect(input).toHaveAttribute(
      "placeholder",
      t("serverDetail.settings.cpuAllocationDefault"),
    );
  });

  it("shows the current allocation when set", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        cpu_millis: 1500,
        config: { cpu_millis: 1500 },
      }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    expect((cpuInput() as HTMLInputElement).value).toBe("1500");
  });

  it("does not show cpu_millis as a raw config override row", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        cpu_millis: 1500,
        config: { cpu_millis: 1500, motd: "hi" },
      }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    expect(screen.getByDisplayValue("motd")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("cpu_millis")).toBeNull();
  });

  it("saves a set CPU allocation into the config blob", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", cpu_millis: null, config: {} }),
    );
    mockApi.patch.mockResolvedValue(server());
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(cpuInput(), { target: { value: "1500" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const config = JSON.parse(mockApi.patch.mock.calls[0][1].body).config;
    expect(config).toEqual({ cpu_millis: 1500 });
    expect(typeof config.cpu_millis).toBe("number");
  });

  it("omits the key from the config blob when the allocation is cleared", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        cpu_millis: 1500,
        config: { cpu_millis: 1500, motd: "hi" },
      }),
    );
    mockApi.patch.mockResolvedValue(server());
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(cpuInput(), { target: { value: "" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const config = JSON.parse(mockApi.patch.mock.calls[0][1].body).config;
    expect(config).toEqual({ motd: "hi" });
  });

  it("rejects a below-floor value and blocks the save", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", cpu_millis: null, config: {} }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(cpuInput(), { target: { value: "50" } });

    expect(
      await screen.findByText(t("serverDetail.settings.cpuAllocationRange")),
    ).toBeInTheDocument();
    expect(mockApi.patch).not.toHaveBeenCalled();
  });

  it("rejects an above-ceiling value and blocks the save", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", cpu_millis: null, config: {} }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(cpuInput(), { target: { value: "128001" } });

    expect(
      await screen.findByText(t("serverDetail.settings.cpuAllocationRange")),
    ).toBeInTheDocument();
    expect(mockApi.patch).not.toHaveBeenCalled();
  });

  it("rejects a non-integer value and blocks the save", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", cpu_millis: null, config: {} }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(cpuInput(), { target: { value: "1500.5" } });

    expect(
      await screen.findByText(t("serverDetail.settings.cpuAllocationRange")),
    ).toBeInTheDocument();
    expect(mockApi.patch).not.toHaveBeenCalled();
  });

  it("disables the CPU allocation field without server:update", async () => {
    mockCan = (code) => code !== "server:update";
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", cpu_millis: 1500 }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();
    expect(cpuInput()).toBeDisabled();
  });

  it("surfaces a 422 invalid_cpu_allocation specifically on save", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", cpu_millis: null, config: {} }),
    );
    mockApi.patch.mockRejectedValue(
      new ApiError(422, { reason: "invalid_cpu_allocation" }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();
    fireEvent.change(cpuInput(), { target: { value: "1500" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    expect(
      await screen.findByText(t("serverDetail.error.invalidCpuAllocation")),
    ).toBeInTheDocument();
  });
});

describe("ServerDetailPage header join_hostname (issue #961)", () => {
  it("shows the port badge when join_hostname is null", async () => {
    mockApi.get.mockResolvedValue(
      server({ join_hostname: null, game_port: 25565 }),
    );
    renderPage();

    expect(await screen.findByText(":25565")).toBeInTheDocument();
  });

  it("shows join_hostname as a clickable badge when non-null", async () => {
    mockApi.get.mockResolvedValue(
      server({ join_hostname: "myserver.relay.example.com", game_port: 25565 }),
    );
    renderPage();

    const badge = await screen.findByRole("button", {
      name: "myserver.relay.example.com",
    });
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAttribute("title", "myserver.relay.example.com");
    // Port badge is hidden when join_hostname is shown.
    expect(screen.queryByText(":25565")).not.toBeInTheDocument();
  });

  it("badge shows hostname without a label prefix", async () => {
    mockApi.get.mockResolvedValue(
      server({ join_hostname: "survival.relay.example.com", game_port: 25565 }),
    );
    renderPage();

    // The badge must show just the hostname, not "Join address: hostname".
    const badge = await screen.findByText("survival.relay.example.com");
    expect(badge).toBeInTheDocument();
    expect(badge.textContent).toBe("survival.relay.example.com");
  });

  it("clicking the badge copies via execCommand fallback and shows Copied!", async () => {
    mockApi.get.mockResolvedValue(
      server({ join_hostname: "myserver.relay.example.com" }),
    );
    renderPage();
    await screen.findByText("myserver.relay.example.com");

    // jsdom does not define execCommand; define it so vi.spyOn can wrap it.
    if (!("execCommand" in document)) {
      Object.defineProperty(document, "execCommand", {
        value: () => true,
        writable: true,
        configurable: true,
      });
    }
    const execSpy = vi.spyOn(document, "execCommand").mockReturnValue(true);

    fireEvent.click(screen.getByText("myserver.relay.example.com"));

    expect(execSpy).toHaveBeenCalledWith("copy");
    expect(
      await screen.findByText(t("serverDetail.copiedJoinHostname")),
    ).toBeInTheDocument();

    execSpy.mockRestore();
  });

  it("badge reverts to hostname when copy fails (no error state)", async () => {
    mockApi.get.mockResolvedValue(
      server({ join_hostname: "myserver.relay.example.com" }),
    );
    renderPage();
    await screen.findByText("myserver.relay.example.com");

    if (!("execCommand" in document)) {
      Object.defineProperty(document, "execCommand", {
        value: () => false,
        writable: true,
        configurable: true,
      });
    }
    const execSpy = vi.spyOn(document, "execCommand").mockReturnValue(false);

    fireEvent.click(screen.getByText("myserver.relay.example.com"));

    // On failure the badge stays showing the hostname (no error state).
    expect(screen.getByText("myserver.relay.example.com")).toBeInTheDocument();

    execSpy.mockRestore();
  });

  it("Copied! does not stick permanently when a re-click fails (issue #976)", async () => {
    mockApi.get.mockResolvedValue(
      server({ join_hostname: "myserver.relay.example.com" }),
    );
    renderPage();
    await screen.findByText("myserver.relay.example.com");

    if (!("execCommand" in document)) {
      Object.defineProperty(document, "execCommand", {
        value: () => true,
        writable: true,
        configurable: true,
      });
    }
    const execSpy = vi.spyOn(document, "execCommand").mockReturnValue(true);

    // Click 1 succeeds — badge shows "Copied!".
    fireEvent.click(screen.getByText("myserver.relay.example.com"));
    expect(
      await screen.findByText(t("serverDetail.copiedJoinHostname")),
    ).toBeInTheDocument();

    // Click 2 fails while "Copied!" is still showing.
    execSpy.mockReturnValue(false);
    fireEvent.click(screen.getByText(t("serverDetail.copiedJoinHostname")));

    // The badge must revert to the hostname (not stay stuck on "Copied!").
    expect(
      await screen.findByText("myserver.relay.example.com"),
    ).toBeInTheDocument();

    execSpy.mockRestore();
  });

  it("badge is keyboard-accessible (native button)", async () => {
    mockApi.get.mockResolvedValue(
      server({ join_hostname: "myserver.relay.example.com" }),
    );
    renderPage();

    // A <button> is natively focusable and activates on Enter/Space.
    const badge = await screen.findByRole("button", {
      name: "myserver.relay.example.com",
    });
    expect(badge.tagName).toBe("BUTTON");
  });
});

describe("ServerDetailPage header join address display (issue #982)", () => {
  it("shows hostname only — no port substring — when join_hostname is set", async () => {
    mockApi.get.mockResolvedValue(
      server({ join_hostname: "survival.relay.example.com", game_port: 25565 }),
    );
    renderPage();

    await screen.findByText("survival.relay.example.com");
    // The port must not appear anywhere in the header when relay is on.
    expect(screen.queryByText(/:25565/)).not.toBeInTheDocument();
    expect(screen.queryByText("25565")).not.toBeInTheDocument();
  });
});

describe("ServerDetailPage header Bedrock address badge (issue #1543)", () => {
  it("shows the Bedrock badge when bedrock_port is set", async () => {
    mockApi.get.mockResolvedValue(
      server({ bedrock_address: "play.example.com", bedrock_port: 19132 }),
    );
    renderPage();

    const badge = await screen.findByRole("button", {
      name: `${t("serverDetail.bedrockLabel")}: play.example.com:19132`,
    });
    expect(badge).toBeInTheDocument();
    // Tooltip copies the host only and points the port at Bedrock's Port field.
    expect(badge).toHaveAttribute(
      "title",
      t("serverDetail.bedrockAddressCopyTitle", { port: 19132 }),
    );
  });

  it("hides the Bedrock badge when bedrock_port is null", async () => {
    mockApi.get.mockResolvedValue(
      server({ bedrock_address: null, bedrock_port: null }),
    );
    renderPage();

    await screen.findByText("survival");
    expect(
      screen.queryByRole("button", {
        name: new RegExp(t("serverDetail.bedrockLabel")),
      }),
    ).not.toBeInTheDocument();
  });

  it("Java badge is unchanged when the Bedrock badge is also shown", async () => {
    mockApi.get.mockResolvedValue(
      server({
        join_hostname: "survival.relay.example.com",
        bedrock_address: "play.example.com",
        bedrock_port: 19132,
      }),
    );
    renderPage();

    expect(
      await screen.findByRole("button", {
        name: "survival.relay.example.com",
      }),
    ).toBeInTheDocument();
  });

  it("clicking the Bedrock badge copies the host only and shows Copied!", async () => {
    mockApi.get.mockResolvedValue(
      server({ bedrock_address: "play.example.com", bedrock_port: 19132 }),
    );
    renderPage();
    const badge = await screen.findByRole("button", {
      name: `${t("serverDetail.bedrockLabel")}: play.example.com:19132`,
    });

    if (!("execCommand" in document)) {
      Object.defineProperty(document, "execCommand", {
        value: () => true,
        writable: true,
        configurable: true,
      });
    }
    // Capture the value handed to the clipboard fallback textarea: it must be
    // the bare host with no `:port` (Bedrock's Port field is separate).
    let copiedText: string | null = null;
    const execSpy = vi
      .spyOn(document, "execCommand")
      .mockImplementation((command) => {
        if (command === "copy") {
          const areas = document.querySelectorAll("textarea");
          copiedText = areas[areas.length - 1]?.value ?? null;
        }
        return true;
      });

    fireEvent.click(badge);

    expect(execSpy).toHaveBeenCalledWith("copy");
    expect(copiedText).toBe("play.example.com");
    expect(
      await screen.findByText(t("serverDetail.copiedBedrockAddress")),
    ).toBeInTheDocument();

    execSpy.mockRestore();
  });
});

describe("ServerDetailPage settings slug (issue #961)", () => {
  let restoreWs: () => void;

  beforeEach(() => {
    restoreWs = installMockWebSocket();
  });
  afterEach(() => {
    restoreWs();
  });

  function openSettings() {
    fireEvent.click(
      screen.getByRole("tab", { name: t("serverDetail.tab.settings") }),
    );
  }

  it("hides the slug field when relay is disabled (join_hostname null)", async () => {
    mockApi.get.mockResolvedValue(
      server({ join_hostname: null, slug: "survival" }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    // The slug field is aria-labelled; when relay is off it must not appear.
    expect(
      screen.queryByLabelText(t("serverDetail.settings.slug")),
    ).not.toBeInTheDocument();
  });

  it("shows the slug field when relay is enabled (join_hostname non-null)", async () => {
    mockApi.get.mockResolvedValue(
      server({ join_hostname: "survival.relay.example.com", slug: "survival" }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    expect(
      screen.getByLabelText(t("serverDetail.settings.slug")),
    ).toBeInTheDocument();
  });

  it("shows inline error for invalid slug format", async () => {
    mockApi.get.mockResolvedValue(
      server({ join_hostname: "survival.relay.example.com", slug: "survival" }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    const slugInput = screen.getByLabelText(t("serverDetail.settings.slug"));
    fireEvent.change(slugInput, { target: { value: "-bad-slug" } });

    expect(
      await screen.findByText(t("serverDetail.settings.slugInvalid")),
    ).toBeInTheDocument();
  });

  it("disables save button when slug is invalid", async () => {
    mockApi.get.mockResolvedValue(
      server({ join_hostname: "survival.relay.example.com", slug: "survival" }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    const slugInput = screen.getByLabelText(t("serverDetail.settings.slug"));
    fireEvent.change(slugInput, { target: { value: "-bad" } });

    const saveBtn = screen.getByRole("button", {
      name: t("serverDetail.settings.save"),
    });
    expect(saveBtn).toBeDisabled();
  });

  it("includes slug in PATCH when changed", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        join_hostname: "survival.relay.example.com",
        slug: "survival",
      }),
    );
    mockApi.patch.mockResolvedValue(server());
    renderPage();

    await screen.findByText("survival");
    openSettings();

    const slugInput = screen.getByLabelText(t("serverDetail.settings.slug"));
    fireEvent.change(slugInput, { target: { value: "new-slug" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const body = JSON.parse(mockApi.patch.mock.calls[0][1].body);
    expect(body.slug).toBe("new-slug");
  });

  it("omits slug from PATCH when unchanged", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        join_hostname: "survival.relay.example.com",
        slug: "survival",
      }),
    );
    mockApi.patch.mockResolvedValue(server());
    renderPage();

    await screen.findByText("survival");
    openSettings();
    // Do not change the slug field; just save.
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const body = JSON.parse(mockApi.patch.mock.calls[0][1].body);
    expect(body.slug).toBeUndefined();
  });

  it("surfaces a 409 slug_taken error inline on save", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        join_hostname: "survival.relay.example.com",
        slug: "survival",
      }),
    );
    mockApi.patch.mockRejectedValue(
      new ApiError(409, { reason: "slug_taken" }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    const slugInput = screen.getByLabelText(t("serverDetail.settings.slug"));
    fireEvent.change(slugInput, { target: { value: "taken-slug" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    expect(
      await screen.findByText(t("serverDetail.settings.slugTaken")),
    ).toBeInTheDocument();
  });

  it("surfaces a 422 invalid_slug error inline on save", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "stopped",
        join_hostname: "survival.relay.example.com",
        slug: "survival",
      }),
    );
    mockApi.patch.mockRejectedValue(
      new ApiError(422, { reason: "invalid_slug" }),
    );
    renderPage();

    await screen.findByText("survival");
    openSettings();

    const slugInput = screen.getByLabelText(t("serverDetail.settings.slug"));
    fireEvent.change(slugInput, { target: { value: "reserved" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.settings.save") }),
    );

    expect(
      await screen.findByText(t("serverDetail.settings.slugInvalid")),
    ).toBeInTheDocument();
  });
});

describe("ServerDetailPage delete (typed confirm)", () => {
  it("deletes after typed confirm and navigates to the dashboard", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "stopped" }));
    mockApi.delete.mockResolvedValue(undefined);
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("tab", { name: t("serverDetail.tab.settings") }),
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("serverDetail.danger.deleteButton"),
      }),
    );

    const confirm = screen.getByRole("button", {
      name: t("serverDetail.delete.confirm"),
    });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText("survival"), {
      target: { value: "survival" },
    });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(mockApi.delete).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}`,
      ),
    );
    await waitFor(() =>
      expect(navigateMock).toHaveBeenCalledWith(`/communities/${CID}`),
    );
  });

  it("hides the delete control without server:delete", async () => {
    mockCan = (code) => code !== "server:delete";
    mockApi.get.mockResolvedValue(server({ observed_state: "stopped" }));
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("tab", { name: t("serverDetail.tab.settings") }),
    );
    expect(
      screen.queryByRole("button", {
        name: t("serverDetail.danger.deleteButton"),
      }),
    ).not.toBeInTheDocument();
  });
});

// ── Overview live streams + Console tab (issue #440) ─────────────────────────

function serverFrame(stream: string, payload: unknown) {
  return JSON.stringify({ stream, ts: "t", payload });
}

describe("ServerDetailPage Overview live streams", () => {
  let restoreWs: () => void;

  beforeEach(() => {
    restoreWs = installMockWebSocket();
  });
  afterEach(() => {
    restoreWs();
  });

  it("renders metrics from the stream and the log tail", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();
    await screen.findByText("survival");

    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        serverFrame("metrics", {
          cpu_millis: 1500,
          memory_bytes: 2 * 1024 * 1024,
          player_count: 4,
        }),
      );
      MockWebSocket.last().message(
        serverFrame("log", { line: "starting up", stream: "stdout" }),
      );
    });

    // CPU 1500 milli-cores → 1.5 cores; players 4; memory 2 MiB.
    expect(screen.getByText("1.5")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
    expect(screen.getByText("starting up")).toBeInTheDocument();
  });

  it("shows a collecting state until the first metrics frame", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();
    await screen.findByText("survival");

    act(() => {
      MockWebSocket.last().open();
    });
    // No metrics frame yet: a clear collecting state, not an empty/zero value.
    expect(
      screen.getByText(t("serverDetail.metric.collecting")),
    ).toBeInTheDocument();

    act(() => {
      MockWebSocket.last().message(
        serverFrame("metrics", {
          cpu_millis: 1500,
          memory_bytes: 2 * 1024 * 1024,
          player_count: 4,
        }),
      );
    });
    // First frame renders the value immediately (no N-sample gate).
    expect(screen.getByText("1.5")).toBeInTheDocument();
    expect(
      screen.queryByText(t("serverDetail.metric.collecting")),
    ).not.toBeInTheDocument();
  });

  it("shows the collecting state while the server is starting", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "starting" }));
    renderPage();
    await screen.findByText("survival");

    // Coming up but no frame yet: honest "collecting", not the stopped copy.
    expect(
      screen.getByText(t("serverDetail.metric.collecting")),
    ).toBeInTheDocument();
  });

  it("says metrics are unavailable while the server is stopped", async () => {
    mockApi.get.mockResolvedValue(
      server({ observed_state: "stopped", desired_state: "stopped" }),
    );
    renderPage();
    await screen.findByText("survival");

    expect(screen.getByText(t("serverDetail.metric.idle"))).toBeInTheDocument();
  });

  it("drops stale metrics and shows idle when the server stops mid-view", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();
    await screen.findByText("survival");

    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        serverFrame("metrics", {
          cpu_millis: 1500,
          memory_bytes: 2 * 1024 * 1024,
          player_count: 4,
        }),
      );
    });
    expect(screen.getByText("1.5")).toBeInTheDocument();

    // The server stops: the strip falls back to idle, not the frozen value.
    act(() => {
      MockWebSocket.last().message(
        serverFrame("status", { state: "stopped", detail: "" }),
      );
    });
    expect(screen.queryByText("1.5")).not.toBeInTheDocument();
    expect(screen.getByText(t("serverDetail.metric.idle"))).toBeInTheDocument();
  });

  it("switches to the Console tab via the tail link", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();
    await screen.findByText("survival");

    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.openConsole") }),
    );
    expect(
      screen.getByRole("checkbox", { name: t("serverDetail.console.follow") }),
    ).toBeInTheDocument();
  });

  it("shows the degraded indicator when the socket drops", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();
    await screen.findByText("survival");

    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().fail();
    });
    expect(screen.getByText(t("dashboard.liveDegraded"))).toBeInTheDocument();
  });

  it("shows the crash detail when a crashed status frame carries a detail string", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();
    await screen.findByText("survival");

    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        serverFrame("status", {
          state: "crashed",
          detail: "container exited unexpectedly",
        }),
      );
    });

    expect(screen.getByText(t("serverDetail.crashDetail"))).toBeInTheDocument();
    expect(
      screen.getByText("container exited unexpectedly"),
    ).toBeInTheDocument();
  });

  it("hides the crash detail when the server recovers from crashed to running", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "stopped" }));
    renderPage();
    await screen.findByText("survival");

    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        serverFrame("status", {
          state: "crashed",
          detail: "forge install produced no args file",
        }),
      );
    });
    expect(
      screen.getByText("forge install produced no args file"),
    ).toBeInTheDocument();

    act(() => {
      MockWebSocket.last().message(
        serverFrame("status", { state: "running", detail: "" }),
      );
    });
    expect(
      screen.queryByText("forge install produced no args file"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(t("serverDetail.crashDetail")),
    ).not.toBeInTheDocument();
  });

  it("does not show crash detail for a running server even if detail is non-empty", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();
    await screen.findByText("survival");

    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        serverFrame("status", {
          state: "running",
          detail: "some info",
        }),
      );
    });
    expect(screen.queryByText("some info")).not.toBeInTheDocument();
    expect(
      screen.queryByText(t("serverDetail.crashDetail")),
    ).not.toBeInTheDocument();
  });

  it("shows crash guidance banner with View Console link when server is crashed", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();
    await screen.findByText("survival");

    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        serverFrame("status", { state: "crashed", detail: "" }),
      );
    });

    expect(
      screen.getByText(t("serverDetail.crashBanner.guidance")),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: t("serverDetail.crashBanner.viewConsole"),
      }),
    ).toBeInTheDocument();
  });

  it("switches to the Console tab when the crash banner View Console link is clicked", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    renderPage();
    await screen.findByText("survival");

    act(() => {
      MockWebSocket.last().open();
      MockWebSocket.last().message(
        serverFrame("status", { state: "crashed", detail: "" }),
      );
    });

    fireEvent.click(
      screen.getByRole("button", {
        name: t("serverDetail.crashBanner.viewConsole"),
      }),
    );

    // The Console tab content is now visible.
    expect(
      screen.getByRole("checkbox", { name: t("serverDetail.console.follow") }),
    ).toBeInTheDocument();
  });

  it("labels the Start button as Restart when the server is crashed", async () => {
    mockApi.get.mockResolvedValue(
      server({
        observed_state: "crashed",
        desired_state: "stopped",
      }),
    );
    renderPage();
    await screen.findByText("survival");

    expect(
      screen.getByRole("button", {
        name: t("serverDetail.startCrashed"),
      }),
    ).toBeInTheDocument();
  });
});

describe("ServerDetailPage Console tab", () => {
  let restoreWs: () => void;

  beforeEach(() => {
    restoreWs = installMockWebSocket();
  });
  afterEach(() => {
    restoreWs();
  });

  async function openConsole(overrides: Record<string, unknown> = {}) {
    mockApi.get.mockResolvedValue(server(overrides));
    renderPage();
    await screen.findByText("survival");
    fireEvent.click(
      screen.getByRole("tab", { name: t("serverDetail.tab.console") }),
    );
  }

  it("shows the empty-state placeholder when the stream has no output", async () => {
    await openConsole({ observed_state: "running" });
    expect(
      screen.getByText(t("serverDetail.logTailEmpty")),
    ).toBeInTheDocument();
  });

  it("hides the empty-state placeholder once the stream has output", async () => {
    await openConsole({ observed_state: "running" });
    act(() => {
      MockWebSocket.last().message(
        serverFrame("log", { line: "hello world", stream: "stdout" }),
      );
    });
    expect(screen.getByText("hello world")).toBeInTheDocument();
    expect(
      screen.queryByText(t("serverDetail.logTailEmpty")),
    ).not.toBeInTheDocument();
  });

  it("renders the gap marker as a missed-events divider", async () => {
    await openConsole({ observed_state: "running" });
    act(() => {
      MockWebSocket.last().message(serverFrame("gap", {}));
    });
    expect(
      screen.getByText(t("serverDetail.missedEvents")),
    ).toBeInTheDocument();
  });

  it("filters the stream by the text filter", async () => {
    await openConsole({ observed_state: "running" });
    act(() => {
      MockWebSocket.last().message(
        serverFrame("log", { line: "keep me", stream: "stdout" }),
      );
      MockWebSocket.last().message(
        serverFrame("log", { line: "drop this", stream: "stdout" }),
      );
    });
    fireEvent.change(
      screen.getByPlaceholderText(t("serverDetail.console.filter")),
      { target: { value: "keep" } },
    );
    expect(screen.getByText("keep me")).toBeInTheDocument();
    expect(screen.queryByText("drop this")).not.toBeInTheDocument();
  });

  it("filters against §-stripped text, matching what is displayed", async () => {
    await openConsole({ observed_state: "running" });
    act(() => {
      MockWebSocket.last().message(
        serverFrame("log", {
          line: " §8- §afloodgate§r, §aGeyser-Spigot",
          stream: "stdout",
        }),
      );
      MockWebSocket.last().message(
        serverFrame("log", { line: "unrelated line", stream: "stdout" }),
      );
    });
    fireEvent.change(
      screen.getByPlaceholderText(t("serverDetail.console.filter")),
      { target: { value: "floodgate, geyser" } },
    );
    expect(screen.getByText(/floodgate, Geyser-Spigot/)).toBeInTheDocument();
    expect(screen.queryByText("unrelated line")).not.toBeInTheDocument();
  });

  it("clears the stream", async () => {
    await openConsole({ observed_state: "running" });
    act(() => {
      MockWebSocket.last().message(
        serverFrame("log", { line: "before clear", stream: "stdout" }),
      );
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.console.clear") }),
    );
    expect(screen.queryByText("before clear")).not.toBeInTheDocument();
  });

  it("disables the RCON input with a hint when not running", async () => {
    await openConsole({ observed_state: "stopped" });
    expect(
      screen.getByPlaceholderText(t("serverDetail.console.notRunning")),
    ).toBeDisabled();
    expect(
      screen.getAllByText(t("serverDetail.console.notRunning")).length,
    ).toBeGreaterThan(0);
  });

  it("hides the RCON input without server:command", async () => {
    mockCan = (code) => code !== "server:command";
    await openConsole({ observed_state: "running" });
    expect(
      screen.queryByRole("button", { name: t("serverDetail.console.send") }),
    ).not.toBeInTheDocument();
  });

  it("sends a command and echoes the command + output distinctly", async () => {
    mockApi.post.mockResolvedValue({ output: "There are 0 players" });
    await openConsole({ observed_state: "running" });

    const input = screen.getByPlaceholderText(
      t("serverDetail.console.commandPlaceholder"),
    );
    fireEvent.change(input, { target: { value: "list" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.console.send") }),
    );

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/command`,
        { body: JSON.stringify({ line: "list" }) },
      ),
    );
    expect(await screen.findByText("There are 0 players")).toBeInTheDocument();
    expect(screen.getByText(/list/)).toBeInTheDocument();
  });

  it("recalls the last command with ArrowUp", async () => {
    mockApi.post.mockResolvedValue({ output: "ok" });
    await openConsole({ observed_state: "running" });

    const input = screen.getByPlaceholderText(
      t("serverDetail.console.commandPlaceholder"),
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "say hi" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());

    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(input.value).toBe("say hi");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(input.value).toBe("");
  });
});

describe("sparklinePoints", () => {
  it("scales a series to the viewbox, inverting y so high reads high", () => {
    // min=0 max=10 over width 100 / height 20, three samples → x at 0,50,100.
    // y inverts: value 0 → bottom (20), value 10 → top (0), value 5 → mid (10).
    expect(sparklinePoints([0, 10, 5], 100, 20)).toBe(
      "0.0,20.0 50.0,0.0 100.0,10.0",
    );
  });

  it("flattens a constant series to the baseline without dividing by zero", () => {
    expect(sparklinePoints([4, 4, 4], 100, 20)).toBe(
      "0.0,20.0 50.0,20.0 100.0,20.0",
    );
  });
});
