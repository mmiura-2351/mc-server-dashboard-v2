import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
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
const BID = "b9";

const mockApi = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  postForm: vi.fn(),
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

// Use the real router (incl. useNavigate): switching to the Backups tab now
// drives the URL hash (#514), so navigate must update the location, not no-op.

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

function backup(overrides: Record<string, unknown> = {}) {
  return {
    id: BID,
    server_id: SID,
    source: "manual",
    health: "healthy",
    size_bytes: 1610612736,
    created_by: "miura",
    created_at: "2026-06-06T04:00:00Z",
    ...overrides,
  };
}

const STATS = {
  count: 2,
  total_bytes: 1610612736,
  unknown_size_count: 0,
  newest: "2026-06-06T04:00:00Z",
  oldest: "2026-06-04T00:00:00Z",
};

// Route api.get by path: server detail, backups list, statistics.
function routeGet(
  opts: {
    srv?: Record<string, unknown>;
    backups?: Record<string, unknown>[];
    stats?: typeof STATS;
  } = {},
) {
  const srv = server(opts.srv);
  const list = { backups: opts.backups ?? [backup()] };
  const stats = opts.stats ?? STATS;
  mockApi.get.mockImplementation((path: string) => {
    if (path.endsWith("/backups/statistics")) {
      return Promise.resolve(stats);
    }
    if (path.endsWith("/backups")) {
      return Promise.resolve(list);
    }
    return Promise.resolve(srv);
  });
}

function renderPage() {
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

async function openBackups() {
  renderPage();
  await screen.findByText("survival");
  fireEvent.click(
    screen.getByRole("tab", { name: t("serverDetail.tab.backups") }),
  );
}

let restoreWs: () => void;
beforeEach(() => {
  setAccessToken("tok-1");
  mockApi.get.mockReset();
  mockApi.post.mockReset();
  mockApi.postForm.mockReset();
  mockApi.patch.mockReset();
  mockApi.delete.mockReset();
  mockDownload.downloadFile.mockReset();
  mockCan = () => true;
  // The detail page opens a per-server events socket; without a mock the events
  // client onDown fires invalidate and refetches mid-test (prior CI flakes).
  restoreWs = installMockWebSocket();
});

afterEach(() => {
  restoreWs();
  vi.clearAllMocks();
});

describe("ServerBackupsTab stats + table", () => {
  it("renders the stats header and a backup row", async () => {
    routeGet();
    await openBackups();

    // "1.5 GiB" appears for both the total-size stat and the single row.
    expect((await screen.findAllByText("1.5 GiB")).length).toBe(2);
    expect(screen.getByText("manual")).toBeInTheDocument();
    // The creator cell carries the full value as a hover title (#519).
    expect(screen.getByText("miura").closest("td")).toHaveAttribute(
      "title",
      "miura",
    );
    // The stats header labels render (count uses "Backups", scoped to the strip).
    expect(screen.getByText(t("backups.stat.totalSize"))).toBeInTheDocument();
    expect(screen.getByText(t("backups.stat.newest"))).toBeInTheDocument();
  });

  it("formats the created-at timestamp and shortens a UUID author (#644)", async () => {
    const authorId = "ad1051a7-1234-5678-9abc-def012345678";
    const createdAt = "2026-06-05T13:46:35.411582Z";
    routeGet({
      backups: [backup({ created_by: authorId, created_at: createdAt })],
    });
    await openBackups();

    // The raw ISO no longer leaks: rendered via toLocaleString (no "T", no
    // microseconds).
    const formatted = new Date(createdAt).toLocaleString();
    expect(await screen.findByText(formatted)).toBeInTheDocument();
    expect(screen.queryByText(createdAt)).not.toBeInTheDocument();

    // A UUID author is abbreviated, full id in the cell title.
    expect(screen.getByText("ad1051a7").closest("td")).toHaveAttribute(
      "title",
      authorId,
    );
  });

  it("formats the newest/oldest stat timestamps (#644)", async () => {
    // Empty list so the formatted stat values cannot collide with a row's
    // created-at cell.
    routeGet({ backups: [] });
    await openBackups();

    expect(
      await screen.findByText(new Date(STATS.newest).toLocaleString()),
    ).toBeInTheDocument();
    expect(
      screen.getByText(new Date(STATS.oldest).toLocaleString()),
    ).toBeInTheDocument();
  });

  it("renders an unknown-size row and flags the total as partial", async () => {
    // A legacy NULL-size backup (predates size tracking, #281): the row shows
    // "unknown" and the total-size stat is flagged as a partial sum so the
    // figure is not silently misread as full usage (#640).
    routeGet({
      backups: [backup(), backup({ id: "b-old", size_bytes: null })],
      stats: { ...STATS, count: 2, unknown_size_count: 1 },
    });
    await openBackups();

    expect(
      await screen.findByText(t("backups.unknownSize")),
    ).toBeInTheDocument();
    expect(
      screen.getByText(`(${t("backups.stat.totalSizePartial")})`),
    ).toBeInTheDocument();
  });

  it("shows an empty-state row when there are no backups", async () => {
    routeGet({ backups: [], stats: { ...STATS, count: 0, total_bytes: 0 } });
    await openBackups();

    expect(await screen.findByText(t("backups.empty"))).toBeInTheDocument();
  });
});

describe("ServerBackupsTab condition badge (#745)", () => {
  it("badges a quarantined backup with a warning condition", async () => {
    routeGet({ backups: [backup({ health: "quarantined" })] });
    await openBackups();

    expect(
      await screen.findByText(t("backups.health.quarantined")),
    ).toBeInTheDocument();
  });

  it("badges an unknown-health backup as unverified", async () => {
    routeGet({ backups: [backup({ health: "unknown" })] });
    await openBackups();

    expect(
      await screen.findByText(t("backups.health.unknown")),
    ).toBeInTheDocument();
  });

  it("shows no condition badge for a healthy backup", async () => {
    routeGet({ backups: [backup({ health: "healthy" })] });
    await openBackups();

    await screen.findByText("manual");
    expect(
      screen.queryByText(t("backups.health.quarantined")),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(t("backups.health.unknown")),
    ).not.toBeInTheDocument();
  });
});

describe("ServerBackupsTab create / upload / download / delete", () => {
  it("creates a backup with a POST to the backups collection", async () => {
    routeGet();
    mockApi.post.mockResolvedValue(backup());
    await openBackups();

    fireEvent.click(
      await screen.findByRole("button", { name: t("backups.create") }),
    );
    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/backups`,
      ),
    );
  });

  it("uploads a chosen file via postForm multipart", async () => {
    routeGet();
    mockApi.postForm.mockResolvedValue(backup());
    await openBackups();

    const input = (await screen.findByLabelText(
      t("backups.upload"),
    )) as HTMLInputElement;
    const file = new File(["x"], "b.tar.gz", { type: "application/gzip" });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => expect(mockApi.postForm).toHaveBeenCalled());
    const [path, form] = mockApi.postForm.mock.calls[0];
    expect(path).toBe(`/api/communities/${CID}/servers/${SID}/backups/upload`);
    expect(form).toBeInstanceOf(FormData);
    expect((form as FormData).get("file")).toBe(file);
  });

  it("downloads a row through the authenticated helper", async () => {
    routeGet();
    mockDownload.downloadFile.mockResolvedValue(undefined);
    await openBackups();

    fireEvent.click(
      await screen.findByRole("button", { name: t("backups.download") }),
    );
    await waitFor(() =>
      expect(mockDownload.downloadFile).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/backups/${BID}/download`,
        `${BID}.tar.gz`,
      ),
    );
  });

  it("deletes after typed confirm with a DELETE to the backup", async () => {
    routeGet();
    mockApi.delete.mockResolvedValue(undefined);
    await openBackups();

    fireEvent.click(
      await screen.findByRole("button", { name: t("backups.delete") }),
    );
    const confirm = screen.getByRole("button", {
      name: t("backups.deleteDialog.confirm"),
    });
    expect(confirm).toBeDisabled();
    fireEvent.change(
      screen.getByPlaceholderText(t("backups.deleteDialog.phrase")),
      { target: { value: "DELETE" } },
    );
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(mockApi.delete).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/backups/${BID}`,
      ),
    );
  });
});

describe("ServerBackupsTab restore (stopped-only two-step)", () => {
  it("blocks restore while running and offers a stop button", async () => {
    routeGet({ srv: { observed_state: "running" } });
    mockApi.post.mockResolvedValue(undefined);
    await openBackups();

    fireEvent.click(
      await screen.findByRole("button", { name: t("backups.restore") }),
    );
    expect(
      screen.getByText(t("backups.restoreDialog.blocked")),
    ).toBeInTheDocument();
    // No typed-confirm restore offered while running; only a stop button.
    expect(
      screen.queryByRole("button", {
        name: t("backups.restoreDialog.confirm"),
      }),
    ).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: t("backups.restoreDialog.stop") }),
    );
    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/stop`,
      ),
    );
  });

  it("restores after typed confirm when stopped", async () => {
    routeGet({ srv: { observed_state: "stopped", desired_state: "stopped" } });
    mockApi.post.mockResolvedValue(undefined);
    await openBackups();

    fireEvent.click(
      await screen.findByRole("button", { name: t("backups.restore") }),
    );
    const confirm = screen.getByRole("button", {
      name: t("backups.restoreDialog.confirm"),
    });
    expect(confirm).toBeDisabled();
    fireEvent.change(
      screen.getByPlaceholderText(t("backups.restoreDialog.phrase")),
      { target: { value: "RESTORE" } },
    );
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/backups/${BID}/restore`,
      ),
    );
  });

  it("gates a quarantined restore behind acknowledgement and sends force=true (#745)", async () => {
    routeGet({
      srv: { observed_state: "stopped", desired_state: "stopped" },
      backups: [backup({ health: "quarantined" })],
    });
    mockApi.post.mockResolvedValue(undefined);
    await openBackups();

    fireEvent.click(
      await screen.findByRole("button", { name: t("backups.restore") }),
    );
    // The damaged-data warning is shown and the confirm uses the force label.
    expect(
      screen.getByText(t("backups.restoreDialog.damagedWarning")),
    ).toBeInTheDocument();
    const confirm = screen.getByRole("button", {
      name: t("backups.restoreDialog.damagedConfirm"),
    });

    // Typing the phrase alone is not enough — the extra acknowledgement gates it.
    fireEvent.change(
      screen.getByPlaceholderText(t("backups.restoreDialog.phrase")),
      { target: { value: "RESTORE" } },
    );
    expect(confirm).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox"));
    expect(confirm).not.toBeDisabled();
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/backups/${BID}/restore?force=true`,
      ),
    );
  });

  it("restores a healthy backup without force and with no extra acknowledgement", async () => {
    routeGet({
      srv: { observed_state: "stopped", desired_state: "stopped" },
      backups: [backup({ health: "healthy" })],
    });
    mockApi.post.mockResolvedValue(undefined);
    await openBackups();

    fireEvent.click(
      await screen.findByRole("button", { name: t("backups.restore") }),
    );
    // No damaged warning, no acknowledgement checkbox on a healthy backup.
    expect(
      screen.queryByText(t("backups.restoreDialog.damagedWarning")),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();

    fireEvent.change(
      screen.getByPlaceholderText(t("backups.restoreDialog.phrase")),
      { target: { value: "RESTORE" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("backups.restoreDialog.confirm") }),
    );

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/backups/${BID}/restore`,
      ),
    );
  });

  it("hides the stop button without server:stop and explains instead", async () => {
    // The one-click stop hits the lifecycle stop endpoint (server:stop); without
    // it the user must ask an operator to stop the server.
    mockCan = (code) => code !== "server:stop";
    routeGet({ srv: { observed_state: "running" } });
    await openBackups();

    fireEvent.click(
      await screen.findByRole("button", { name: t("backups.restore") }),
    );
    expect(
      screen.getByText(t("backups.restoreDialog.blockedNoStop")),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("backups.restoreDialog.stop") }),
    ).not.toBeInTheDocument();
  });

  it("surfaces a 409 server_not_stopped specifically on restore", async () => {
    routeGet({ srv: { observed_state: "stopped", desired_state: "stopped" } });
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "server_not_stopped" }),
    );
    await openBackups();

    fireEvent.click(
      await screen.findByRole("button", { name: t("backups.restore") }),
    );
    fireEvent.change(
      screen.getByPlaceholderText(t("backups.restoreDialog.phrase")),
      { target: { value: "RESTORE" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("backups.restoreDialog.confirm") }),
    );

    expect(
      await screen.findByText(t("backups.error.notStopped")),
    ).toBeInTheDocument();
  });
});

describe("ServerBackupsTab schedule field", () => {
  it("PATCHes backup_interval_hours as a NUMBER", async () => {
    routeGet({ srv: { config: { motd: "hi" } } });
    mockApi.patch.mockResolvedValue(server());
    await openBackups();

    const input = (await screen.findByLabelText(
      t("backups.schedule.label"),
    )) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "24" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("backups.schedule.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const [path, init] = mockApi.patch.mock.calls[0];
    expect(path).toBe(`/api/communities/${CID}/servers/${SID}`);
    const config = JSON.parse(init.body).config;
    // Existing keys round-trip; the new key saves as a number, not "24".
    expect(config).toEqual({ motd: "hi", backup_interval_hours: 24 });
    expect(typeof config.backup_interval_hours).toBe("number");
  });

  it("removes the key when the field is cleared", async () => {
    routeGet({ srv: { config: { backup_interval_hours: 12, motd: "hi" } } });
    mockApi.patch.mockResolvedValue(server());
    await openBackups();

    const input = (await screen.findByDisplayValue("12")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("backups.schedule.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const config = JSON.parse(mockApi.patch.mock.calls[0][1].body).config;
    expect(config).toEqual({ motd: "hi" });
  });

  it("lets a backup:schedule-only user edit and save (no server:update)", async () => {
    // The API branches the PATCH gate by the changed-key set: a cadence-only edit
    // needs backup:schedule, not server:update (issue #458). So a holder of only
    // backup:schedule gets an editable field and a live Save control.
    mockCan = (code) => code !== "server:update";
    routeGet({ srv: { config: { backup_interval_hours: 12 } } });
    mockApi.patch.mockResolvedValue(server());
    await openBackups();

    const input = (await screen.findByLabelText(
      t("backups.schedule.label"),
    )) as HTMLInputElement;
    expect(input).not.toBeDisabled();
    fireEvent.change(input, { target: { value: "24" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("backups.schedule.save") }),
    );

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalled());
    const config = JSON.parse(mockApi.patch.mock.calls[0][1].body).config;
    expect(config).toEqual({ backup_interval_hours: 24 });
  });

  it("hides the field without backup:schedule", async () => {
    mockCan = (code) => code !== "backup:schedule";
    routeGet({ srv: { config: { backup_interval_hours: 12 } } });
    await openBackups();

    // Wait for the tab to settle, then assert the schedule field is absent.
    await screen.findByText(t("backups.col.created"));
    expect(
      screen.queryByLabelText(t("backups.schedule.label")),
    ).not.toBeInTheDocument();
  });
});

describe("ServerBackupsTab permission gating", () => {
  it("denies the whole tab without backup:read", async () => {
    mockCan = (code) => code !== "backup:read";
    routeGet();
    await openBackups();

    expect(await screen.findByText(t("backups.noRead"))).toBeInTheDocument();
  });

  it("hides create/upload without backup:create", async () => {
    mockCan = (code) => code !== "backup:create";
    routeGet();
    await openBackups();

    await screen.findByText("manual");
    expect(
      screen.queryByRole("button", { name: t("backups.create") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("backups.upload") }),
    ).not.toBeInTheDocument();
  });

  it("hides restore/delete row actions without those permissions", async () => {
    mockCan = (code) => code !== "backup:restore" && code !== "backup:delete";
    routeGet();
    await openBackups();

    const row = (await screen.findByText("manual")).closest("tr");
    expect(row).not.toBeNull();
    const cell = within(row as HTMLElement);
    expect(
      cell.queryByRole("button", { name: t("backups.restore") }),
    ).not.toBeInTheDocument();
    expect(
      cell.queryByRole("button", { name: t("backups.delete") }),
    ).not.toBeInTheDocument();
    expect(
      cell.getByRole("button", { name: t("backups.download") }),
    ).toBeInTheDocument();
  });

  it("routes a create 403 through the permission glue", async () => {
    routeGet();
    mockApi.post.mockRejectedValue(
      new ApiError(403, { reason: "forbidden", permission: "backup:create" }),
    );
    await openBackups();

    fireEvent.click(
      await screen.findByRole("button", { name: t("backups.create") }),
    );
    expect(
      await screen.findByText(`${t("permissions.deniedNamed")}backup:create`),
    ).toBeInTheDocument();
  });
});
