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
  return render(
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
    mockApi.get.mockResolvedValue(server({ observed_state: "stopped" }));
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

  it("force-stops via the dropdown with ?force=true", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "running" }));
    mockApi.post.mockResolvedValue(server({ observed_state: "stopping" }));
    renderPage();

    await screen.findByText("survival");
    fireEvent.click(screen.getByRole("button", { name: /Stop/ }));
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("serverDetail.stopForce") }),
    );

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/stop?force=true`,
      ),
    );
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

    it("Enter activates the focused item", async () => {
      await openWith("ArrowUp"); // focuses Force stop
      fireEvent.keyDown(items()[1], { key: "Enter" });
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

    it("returns focus to the trigger after keyboard activation (APG)", async () => {
      const trigger = await openWith("Enter");
      fireEvent.keyDown(items()[0], { key: "Enter" });
      expect(
        screen.queryByRole("menuitem", { name: t("serverDetail.stopForce") }),
      ).not.toBeInTheDocument();
      expect(trigger).toHaveFocus();
    });

    it("returns focus to the trigger after click activation (APG)", async () => {
      const trigger = await openWith("Enter");
      fireEvent.click(items()[1]);
      expect(
        screen.queryByRole("menuitem", { name: t("serverDetail.stopForce") }),
      ).not.toBeInTheDocument();
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
      await screen.findByText(`${t("permissions.deniedNamed")}server:restart`),
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
});

describe("ServerDetailPage export", () => {
  it("downloads the export ZIP through the authenticated helper", async () => {
    mockApi.get.mockResolvedValue(server({ observed_state: "stopped" }));
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
    const backend = screen.getByDisplayValue("container");
    expect(backend).toBeDisabled();
    expect(screen.getByDisplayValue("max-players")).toBeInTheDocument();
    expect(screen.getByDisplayValue("hard")).toBeInTheDocument();
  });

  it("checks port availability on blur and shows the taken hint", async () => {
    mockApi.get.mockResolvedValueOnce(server({ observed_state: "stopped" }));
    mockApi.get.mockResolvedValueOnce({
      port: 25570,
      in_range: true,
      available: false,
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
    mockApi.get.mockResolvedValue(server({ observed_state: "stopped" }));
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
