import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  type MockInstance,
  vi,
} from "vitest";
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
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));

const mockPostFormWithProgress = vi.hoisted(() => vi.fn());

vi.mock("../api/client.ts", async () => {
  const actual =
    await vi.importActual<typeof import("../api/client.ts")>(
      "../api/client.ts",
    );
  return {
    ...actual,
    api: mockApi,
    postFormWithProgress: mockPostFormWithProgress,
  };
});

// Keep saveUrlAs real so assertions observe the generated download anchor.
const mockDownload = vi.hoisted(() => ({ downloadFile: vi.fn() }));
vi.mock("../api/download.ts", async () => {
  const actual =
    await vi.importActual<typeof import("../api/download.ts")>(
      "../api/download.ts",
    );
  return { ...actual, ...mockDownload };
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

// Use the real router because tab switching updates the URL hash.

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

// Reject only the requested failing endpoint; server detail must load for the tab to mount.
function routeGet(
  opts: {
    srv?: Record<string, unknown>;
    backups?: Record<string, unknown>[];
    stats?: typeof STATS;
    listError?: unknown;
    statsError?: unknown;
  } = {},
) {
  const srv = server(opts.srv);
  const list = { backups: opts.backups ?? [backup()] };
  const stats = opts.stats ?? STATS;
  mockApi.get.mockImplementation((path: string) => {
    if (path.endsWith("/backups/statistics")) {
      return opts.statsError !== undefined
        ? Promise.reject(opts.statsError)
        : Promise.resolve(stats);
    }
    if (path.endsWith("/backups")) {
      return opts.listError !== undefined
        ? Promise.reject(opts.listError)
        : Promise.resolve(list);
    }
    return Promise.resolve(srv);
  });
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = render(
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
  return { ...result, queryClient };
}

async function openBackups() {
  const { queryClient } = renderPage();
  await screen.findByText("survival");
  fireEvent.click(
    screen.getByRole("tab", { name: t("serverDetail.tab.backups") }),
  );
  return { queryClient };
}

let restoreWs: () => void;
beforeEach(() => {
  setAccessToken("tok-1");
  mockApi.get.mockReset();
  mockApi.post.mockReset();
  mockApi.put.mockReset();
  mockApi.patch.mockReset();
  mockApi.delete.mockReset();
  mockPostFormWithProgress.mockReset();
  mockDownload.downloadFile.mockReset();
  mockCan = () => true;
  // Stub the event socket so disconnect-driven refetches cannot race fixture assertions.
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

    expect((await screen.findAllByText("1.5 GiB")).length).toBe(2);
    expect(screen.getByText("manual")).toBeInTheDocument();
    expect(screen.getByText("miura").closest("td")).toHaveAttribute(
      "title",
      "miura",
    );
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

    const formatted = new Date(createdAt).toLocaleString();
    expect(await screen.findByText(formatted)).toBeInTheDocument();
    expect(screen.queryByText(createdAt)).not.toBeInTheDocument();

    expect(screen.getByText("ad1051a7").closest("td")).toHaveAttribute(
      "title",
      authorId,
    );
  });

  it("shows the resolved author username with the full id in the title (#688)", async () => {
    const authorId = "ad1051a7-1234-5678-9abc-def012345678";
    routeGet({
      backups: [backup({ created_by: authorId, created_by_username: "alice" })],
    });
    await openBackups();

    const cell = (await screen.findByText("alice")).closest("td");
    expect(cell).toHaveAttribute("title", authorId);
    expect(screen.queryByText("ad1051a7")).not.toBeInTheDocument();
  });

  it("formats the newest/oldest stat timestamps (#644)", async () => {
    // Use an empty list so statistics cannot collide with formatted dates in table rows.
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

  it("badges an unreadable backup apart from a damaged one (#2374)", async () => {
    routeGet({ backups: [backup({ health: "unreadable" })] });
    await openBackups();

    const badge = await screen.findByText(t("backups.health.unreadable"));
    expect(badge).toHaveAttribute("title", t("backups.health.unreadableTitle"));
    expect(
      screen.queryByText(t("backups.health.quarantined")),
    ).not.toBeInTheDocument();
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

  it("uploads a chosen file via multipart with progress", async () => {
    routeGet();
    mockPostFormWithProgress.mockResolvedValue(backup());
    await openBackups();

    const input = (await screen.findByLabelText(
      t("backups.upload"),
    )) as HTMLInputElement;
    const file = new File(["x"], "b.tar.gz", { type: "application/gzip" });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
    const [path, form] = mockPostFormWithProgress.mock.calls[0];
    expect(path).toBe(`/api/communities/${CID}/servers/${SID}/backups/upload`);
    expect(form).toBeInstanceOf(FormData);
    expect((form as FormData).get("file")).toBe(file);
  });

  describe("download (minted grant, #2314)", () => {
    const GRANT_URL = `/api/communities/${CID}/servers/${SID}/backups/${BID}/download?grant=jwt-1`;
    const clicks: HTMLAnchorElement[] = [];
    let fetchSpy: MockInstance<typeof fetch>;

    const fetchedUrls = () =>
      fetchSpy.mock.calls.map(([input]) => String(input));

    beforeEach(() => {
      clicks.length = 0;
      // Capture the anchor click without navigating the test document.
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
        function (this: HTMLAnchorElement) {
          clicks.push(this);
        },
      );
      fetchSpy = vi.spyOn(globalThis, "fetch");
    });

    afterEach(() => {
      vi.restoreAllMocks();
    });

    it("mints a grant and saves it under the backup filename", async () => {
      routeGet();
      mockApi.post.mockResolvedValue({
        download_url: GRANT_URL,
        expires_at: "2026-06-06T04:00:30Z",
      });
      await openBackups();

      fireEvent.click(
        await screen.findByRole("button", { name: t("backups.download") }),
      );

      await waitFor(() =>
        expect(mockApi.post).toHaveBeenCalledWith(
          `/api/communities/${CID}/servers/${SID}/backups/${BID}/download-grant`,
        ),
      );
      await waitFor(() => expect(clicks).toHaveLength(1));
      expect(clicks[0].getAttribute("href")).toBe(GRANT_URL);
      expect(clicks[0].download).toBe(`${BID}.tar.gz`);
      expect(mockDownload.downloadFile).not.toHaveBeenCalled();
      expect(fetchedUrls()).not.toContain(GRANT_URL);
      expect(
        fetchedUrls().filter((url) => url.includes("download?grant=")),
      ).toEqual([]);
    });

    it("disables the download button while the mint is in flight", async () => {
      routeGet();
      let settleMint: (grant: { download_url: string }) => void = () => {};
      mockApi.post.mockReturnValue(
        new Promise<{ download_url: string }>((resolve) => {
          settleMint = resolve;
        }),
      );
      await openBackups();

      const button = await screen.findByRole("button", {
        name: t("backups.download"),
      });
      fireEvent.click(button);

      await waitFor(() => expect(button).toBeDisabled());
      fireEvent.click(button);
      expect(mockApi.post).toHaveBeenCalledTimes(1);

      settleMint({ download_url: GRANT_URL });
      await waitFor(() => expect(button).toBeEnabled());
      expect(clicks).toHaveLength(1);
    });

    it("routes a mint 403 through the permission glue", async () => {
      routeGet();
      mockApi.post.mockRejectedValue(
        new ApiError(403, { reason: "forbidden", permission: "backup:read" }),
      );
      await openBackups();

      fireEvent.click(
        await screen.findByRole("button", { name: t("backups.download") }),
      );

      expect(
        await screen.findByText(
          t("permissions.deniedNamed", { permission: "backup:read" }),
        ),
      ).toBeInTheDocument();
      expect(clicks).toHaveLength(0);
    });
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
    expect(
      screen.getByText(t("backups.restoreDialog.damagedWarning")),
    ).toBeInTheDocument();
    const confirm = screen.getByRole("button", {
      name: t("backups.restoreDialog.damagedConfirm"),
    });

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

  it("offers no restore for an unreadable backup, even when stopped (#2374)", async () => {
    routeGet({
      srv: { observed_state: "stopped", desired_state: "stopped" },
      backups: [backup({ health: "unreadable" })],
    });
    await openBackups();

    const restore = await screen.findByRole("button", {
      name: t("backups.restore"),
    });
    expect(restore).toBeDisabled();
    expect(restore).toHaveAttribute(
      "title",
      t("backups.restoreUnavailableUnreadable"),
    );
    fireEvent.click(restore);
    expect(
      screen.queryByPlaceholderText(t("backups.restoreDialog.phrase")),
    ).not.toBeInTheDocument();
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

  it("surfaces a failed stop from the restore dialog", async () => {
    routeGet({ srv: { observed_state: "running" } });
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "server_busy" }),
    );
    await openBackups();

    fireEvent.click(
      await screen.findByRole("button", { name: t("backups.restore") }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("backups.restoreDialog.stop") }),
    );

    expect(
      await screen.findByText(t("backups.error.serverBusy")),
    ).toBeInTheDocument();
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
      (await screen.findByText(t("backups.error.notStopped"))).closest(
        ".toast",
      ),
    ).not.toBeNull();
    expect(
      screen.getByPlaceholderText(t("backups.restoreDialog.phrase")),
    ).toHaveValue("RESTORE");
  });
});

describe("ServerBackupsTab schedule note", () => {
  it("points a backup:schedule holder at the Schedules surface (#1840)", async () => {
    routeGet({ srv: { config: { motd: "hi" } } });
    await openBackups();

    expect(
      await screen.findByText(t("backups.schedule.movedNote")),
    ).toBeInTheDocument();
    expect(mockApi.patch).not.toHaveBeenCalled();
  });

  it("hides the note without backup:schedule", async () => {
    mockCan = (code) => code !== "backup:schedule";
    routeGet();
    await openBackups();

    await screen.findByText(t("backups.col.created"));
    expect(
      screen.queryByText(t("backups.schedule.movedNote")),
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

  it("names the upload limit on a reasonless 413 upload", async () => {
    routeGet();
    mockPostFormWithProgress.mockRejectedValue(new ApiError(413, undefined));
    await openBackups();

    const input = (await screen.findByLabelText(
      t("backups.upload"),
    )) as HTMLInputElement;
    const file = new File(["x"], "b.tar.gz", { type: "application/gzip" });
    fireEvent.change(input, { target: { files: [file] } });

    expect(
      await screen.findByText(t("backups.error.tooLarge")),
    ).toBeInTheDocument();
  });

  it("names the platform-managed path on a 422 platform_managed_path upload (#2790)", async () => {
    routeGet();
    mockPostFormWithProgress.mockRejectedValue(
      new ApiError(422, { reason: "platform_managed_path" }),
    );
    await openBackups();

    const input = (await screen.findByLabelText(
      t("backups.upload"),
    )) as HTMLInputElement;
    const file = new File(["x"], "b.tar.gz", { type: "application/gzip" });
    fireEvent.change(input, { target: { files: [file] } });

    const toast = await screen.findByText(
      t("backups.error.platformManagedPath"),
    );
    expect(toast.textContent).toMatch(/server\.properties directory/);
    expect(toast.textContent).toMatch(/entry under server\.properties\//);
    expect(toast.textContent).toMatch(/upload again/);
    expect(
      screen.queryByText(t("backups.error.generic")),
    ).not.toBeInTheDocument();
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
      await screen.findByText(
        t("permissions.deniedNamed", { permission: "backup:create" }),
      ),
    ).toBeInTheDocument();
  });
});

describe("ServerBackupsTab query error surfacing (#2554)", () => {
  it("surfaces a 503 storage_unavailable on the listing with the specific message", async () => {
    routeGet({
      listError: new ApiError(503, { reason: "storage_unavailable" }),
    });
    await openBackups();

    expect(
      await screen.findByText(t("backups.error.storageUnavailable")),
    ).toBeInTheDocument();
    expect(screen.queryByText(t("backups.loadError"))).not.toBeInTheDocument();
    expect(
      screen.queryByText(t("backups.error.workerUnavailable")),
    ).not.toBeInTheDocument();
  });

  it("falls back to the load message for an unmapped listing failure", async () => {
    routeGet({ listError: new ApiError(500, { reason: "internal_error" }) });
    await openBackups();

    expect(await screen.findByText(t("backups.loadError"))).toBeInTheDocument();
  });

  it("keeps the listing rendered when statistics alone fails", async () => {
    routeGet({
      statsError: new ApiError(503, { reason: "storage_unavailable" }),
    });
    await openBackups();

    expect(await screen.findByText("manual")).toBeInTheDocument();
    expect(
      screen.getByText(t("backups.error.storageUnavailable")),
    ).toBeInTheDocument();
  });
});

describe("ServerBackupsTab retention editor (#1843)", () => {
  function retentionPut(): [string, RequestInit] | undefined {
    return mockApi.put.mock.calls.find((call) =>
      String(call[0]).endsWith("/backups/retention"),
    ) as [string, RequestInit] | undefined;
  }
  function retentionDeleted(): boolean {
    return mockApi.delete.mock.calls.some((call) =>
      String(call[0]).endsWith("/backups/retention"),
    );
  }
  const modeSelect = () =>
    screen.findByLabelText(t("backups.retention.modeLabel"));
  const saveButton = () =>
    screen.getByRole("button", { name: t("backups.retention.save") });

  it("shows the editor for a backup:schedule holder, defaulting to no policy", async () => {
    routeGet();
    await openBackups();

    const mode = (await modeSelect()) as HTMLSelectElement;
    expect(mode.value).toBe("none");
    expect(
      screen.queryByLabelText(t("backups.retention.keepLastLabel")),
    ).not.toBeInTheDocument();
  });

  it("hides the editor without backup:schedule", async () => {
    mockCan = (code) => code !== "backup:schedule";
    routeGet();
    await openBackups();

    await screen.findByText(t("backups.col.created"));
    expect(
      screen.queryByLabelText(t("backups.retention.modeLabel")),
    ).not.toBeInTheDocument();
  });

  it("sets a keep-last policy via PUT", async () => {
    mockApi.put.mockResolvedValue({ keep_last: 7 });
    routeGet();
    await openBackups();

    fireEvent.change(await modeSelect(), { target: { value: "keepLast" } });
    fireEvent.change(
      screen.getByLabelText(t("backups.retention.keepLastLabel")),
      { target: { value: "7" } },
    );
    fireEvent.click(saveButton());

    await waitFor(() => expect(retentionPut()).toBeDefined());
    const [path, init] = retentionPut() as [string, RequestInit];
    expect(path.endsWith(`/servers/${SID}/backups/retention`)).toBe(true);
    expect(JSON.parse(init.body as string)).toEqual({ keep_last: 7 });
    expect(
      await screen.findByText(t("backups.retention.saved")),
    ).toBeInTheDocument();
  });

  it("refreshes backups and stats after a pruning save", async () => {
    // Retention prunes rows before responding; verify the list and statistics are invalidated afterward.
    mockApi.put.mockResolvedValue({ keep_last: 1 });
    routeGet();
    const { queryClient } = await openBackups();

    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    fireEvent.change(await modeSelect(), { target: { value: "keepLast" } });
    fireEvent.change(
      screen.getByLabelText(t("backups.retention.keepLastLabel")),
      { target: { value: "1" } },
    );
    fireEvent.click(saveButton());

    await waitFor(() => expect(retentionPut()).toBeDefined());

    const invalidatedKeys = invalidateSpy.mock.calls.map(
      (call) => (call[0] as { queryKey: readonly unknown[] }).queryKey,
    );
    expect(invalidatedKeys).toContainEqual(["backups", CID, SID]);
    expect(invalidatedKeys).toContainEqual(["backups", CID, SID, "statistics"]);
  });

  it("sets a tiered policy via PUT (empty tier fields count as 0)", async () => {
    mockApi.put.mockResolvedValue({ daily: 7, weekly: 0, monthly: 6 });
    routeGet();
    await openBackups();

    fireEvent.change(await modeSelect(), { target: { value: "tiered" } });
    fireEvent.change(screen.getByLabelText(t("backups.retention.dailyLabel")), {
      target: { value: "7" },
    });
    fireEvent.change(
      screen.getByLabelText(t("backups.retention.monthlyLabel")),
      {
        target: { value: "6" },
      },
    );
    fireEvent.click(saveButton());

    await waitFor(() => expect(retentionPut()).toBeDefined());
    const [, init] = retentionPut() as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      daily: 7,
      weekly: 0,
      monthly: 6,
    });
  });

  it("prefills an existing keep-last policy", async () => {
    routeGet({ srv: { backup_retention: { keep_last: 5 } } });
    await openBackups();

    const mode = (await modeSelect()) as HTMLSelectElement;
    expect(mode.value).toBe("keepLast");
    expect(
      (
        screen.getByLabelText(
          t("backups.retention.keepLastLabel"),
        ) as HTMLInputElement
      ).value,
    ).toBe("5");
  });

  it("prefills an existing tiered policy", async () => {
    routeGet({
      srv: { backup_retention: { daily: 7, weekly: 0, monthly: 3 } },
    });
    await openBackups();

    const mode = (await modeSelect()) as HTMLSelectElement;
    expect(mode.value).toBe("tiered");
    expect(
      (
        screen.getByLabelText(
          t("backups.retention.monthlyLabel"),
        ) as HTMLInputElement
      ).value,
    ).toBe("3");
  });

  it("edits an existing keep-last policy via PUT", async () => {
    mockApi.put.mockResolvedValue({ keep_last: 9 });
    routeGet({ srv: { backup_retention: { keep_last: 5 } } });
    await openBackups();

    await modeSelect();
    fireEvent.change(
      screen.getByLabelText(t("backups.retention.keepLastLabel")),
      { target: { value: "9" } },
    );
    fireEvent.click(saveButton());

    await waitFor(() => expect(retentionPut()).toBeDefined());
    const [, init] = retentionPut() as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ keep_last: 9 });
  });

  it("clears an existing policy via DELETE", async () => {
    mockApi.delete.mockResolvedValue(undefined);
    routeGet({ srv: { backup_retention: { keep_last: 5 } } });
    await openBackups();

    fireEvent.change(await modeSelect(), { target: { value: "none" } });
    fireEvent.click(saveButton());

    await waitFor(() => expect(retentionDeleted()).toBe(true));
    expect(
      await screen.findByText(t("backups.retention.cleared")),
    ).toBeInTheDocument();
  });

  it("rejects an empty keep-last inline without calling the API", async () => {
    routeGet();
    await openBackups();

    fireEvent.change(await modeSelect(), { target: { value: "keepLast" } });
    fireEvent.click(saveButton());

    expect(
      await screen.findByText(t("backups.retention.error.keepLast")),
    ).toBeInTheDocument();
    expect(mockApi.put).not.toHaveBeenCalled();
  });

  it("rejects an all-zero tiered policy inline without calling the API", async () => {
    routeGet();
    await openBackups();

    fireEvent.change(await modeSelect(), { target: { value: "tiered" } });
    fireEvent.click(saveButton());

    expect(
      await screen.findByText(t("backups.retention.error.tiered")),
    ).toBeInTheDocument();
    expect(mockApi.put).not.toHaveBeenCalled();
  });

  it("surfaces a server invalid_retention_policy as an inline error", async () => {
    mockApi.put.mockRejectedValue(
      new ApiError(422, { reason: "invalid_retention_policy" }),
    );
    routeGet();
    await openBackups();

    fireEvent.change(await modeSelect(), { target: { value: "keepLast" } });
    fireEvent.change(
      screen.getByLabelText(t("backups.retention.keepLastLabel")),
      { target: { value: "5" } },
    );
    fireEvent.click(saveButton());

    expect(
      await screen.findByText(t("backups.retention.error.invalid")),
    ).toBeInTheDocument();
  });
});
