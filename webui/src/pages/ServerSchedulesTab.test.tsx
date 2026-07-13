import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { setAccessToken } from "../auth/tokenStore.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import type { PermissionCode } from "../permissions/catalog.ts";
import type { Can } from "../permissions/useCan.ts";
import { ServerSchedulesTab } from "./ServerSchedulesTab.tsx";

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

vi.mock("../permissions/ActiveCommunityProvider.tsx", () => ({
  useActiveCommunity: () => ({
    communityId: CID,
    setCommunityId: vi.fn(),
    communities: [{ id: CID, name: "Sakura" }],
  }),
}));

const ALL_CODES: PermissionCode[] = [
  "schedule:read",
  "schedule:manage",
  "server:command",
  "server:start",
  "server:stop",
  "server:restart",
  "backup:schedule",
];

function canFor(codes: PermissionCode[]): Can {
  return (code) => codes.includes(code);
}

function server() {
  return {
    id: SID,
    community_id: CID,
    name: "survival",
    slug: "survival",
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
    cpu_millis: null,
    memory_limit_mb: null,
  };
}

function schedule(overrides: Record<string, unknown> = {}) {
  return {
    id: "sch1",
    server_id: SID,
    name: "nightly backup",
    action: "backup",
    cron: null,
    interval_seconds: 3600,
    timezone: "UTC",
    enabled: true,
    command: null,
    warning_steps: [],
    next_run_at: "2026-06-07T00:00:00Z",
    last_run_at: "2026-06-06T00:00:00Z",
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    created_by: "miura",
    ...overrides,
  };
}

function run(overrides: Record<string, unknown> = {}) {
  return {
    id: "run1",
    schedule_id: "sch1",
    started_at: "2026-06-06T00:00:00Z",
    finished_at: "2026-06-06T00:00:05Z",
    outcome: "failure",
    detail: "worker_unavailable",
    ...overrides,
  };
}

// Route api.get by path: schedule list vs. run history.
function routeGet(opts: { schedules?: object[]; runs?: object[] } = {}) {
  mockApi.get.mockImplementation((path: string) => {
    if (path.endsWith("/runs")) {
      return Promise.resolve(opts.runs ?? [run()]);
    }
    return Promise.resolve(opts.schedules ?? [schedule()]);
  });
}

function renderTab(can: Can) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <ServerSchedulesTab server={server()} communityId={CID} can={can} />
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
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("ServerSchedulesTab table", () => {
  it("renders a schedule row with humanized cadence and next/last run", async () => {
    routeGet();
    renderTab(canFor(ALL_CODES));

    expect(await screen.findByText("nightly backup")).toBeInTheDocument();
    expect(screen.getByText(t("schedules.action.backup"))).toBeInTheDocument();
    // 3600s interval humanizes to "Every 1 h".
    expect(
      screen.getByText(t("schedules.cadence.everyHours", { count: 1 })),
    ).toBeInTheDocument();
    expect(screen.getByText("UTC")).toBeInTheDocument();
    // Enabled toggle reflects the schedule state.
    expect(
      screen.getByLabelText(
        t("schedules.enabledLabel", { name: "nightly backup" }),
      ),
    ).toBeChecked();
  });

  it("humanizes a cron cadence", async () => {
    routeGet({
      schedules: [schedule({ interval_seconds: null, cron: "*/5 * * * *" })],
    });
    renderTab(canFor(ALL_CODES));

    expect(
      await screen.findByText(
        t("schedules.cadence.cron", { cron: "*/5 * * * *" }),
      ),
    ).toBeInTheDocument();
  });

  it("shows the empty state when there are no schedules", async () => {
    routeGet({ schedules: [] });
    renderTab(canFor(ALL_CODES));

    expect(await screen.findByText(t("schedules.empty"))).toBeInTheDocument();
  });
});

describe("ServerSchedulesTab permissions", () => {
  it("denies the read-only message without schedule:read", () => {
    renderTab(canFor([]));
    expect(screen.getByText(t("schedules.noRead"))).toBeInTheDocument();
    expect(mockApi.get).not.toHaveBeenCalled();
  });

  it("gives a schedule:read-only member a read-only view", async () => {
    routeGet();
    renderTab(canFor(["schedule:read"]));

    await screen.findByText("nightly backup");
    // No create / edit / delete affordances.
    expect(
      screen.queryByRole("button", { name: t("schedules.create") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("schedules.edit") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("schedules.delete") }),
    ).not.toBeInTheDocument();
    // Run history stays available; the enabled toggle is read-only.
    expect(
      screen.getByRole("button", { name: t("schedules.history") }),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText(
        t("schedules.enabledLabel", { name: "nightly backup" }),
      ),
    ).toBeDisabled();
  });

  it("hides create when schedule:manage holds no action permission", async () => {
    routeGet();
    renderTab(canFor(["schedule:read", "schedule:manage"]));

    await screen.findByText("nightly backup");
    expect(
      screen.queryByRole("button", { name: t("schedules.create") }),
    ).not.toBeInTheDocument();
  });

  it("offers only the permitted actions in the create dialog", async () => {
    routeGet({ schedules: [] });
    renderTab(canFor(["schedule:read", "schedule:manage", "server:start"]));

    fireEvent.click(
      await screen.findByRole("button", { name: t("schedules.create") }),
    );
    const select = screen.getByLabelText(t("schedules.dialog.actionLabel"));
    const options = within(select).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual([
      t("schedules.action.start"),
    ]);
  });

  it("hides edit/delete for a row whose action the caller cannot run", async () => {
    // Holds manage but only start — the backup-action row is not writable.
    routeGet();
    renderTab(canFor(["schedule:read", "schedule:manage", "server:start"]));

    await screen.findByText("nightly backup");
    expect(
      screen.queryByRole("button", { name: t("schedules.edit") }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByLabelText(
        t("schedules.enabledLabel", { name: "nightly backup" }),
      ),
    ).toBeDisabled();
  });
});

describe("ServerSchedulesTab create/edit dialog", () => {
  it("shows the warning editor only for stop/restart actions", async () => {
    routeGet({ schedules: [] });
    renderTab(canFor(ALL_CODES));

    fireEvent.click(
      await screen.findByRole("button", { name: t("schedules.create") }),
    );
    const select = screen.getByLabelText(t("schedules.dialog.actionLabel"));

    // Default action is "command": no warnings, but a command field.
    expect(
      screen.queryByText(t("schedules.dialog.warningsLabel")),
    ).not.toBeInTheDocument();
    expect(
      screen.getByLabelText(t("schedules.dialog.commandLabel")),
    ).toBeInTheDocument();

    // Switch to stop: warnings editor appears, command field goes away.
    fireEvent.change(select, { target: { value: "stop" } });
    expect(
      screen.getByText(t("schedules.dialog.warningsLabel")),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText(t("schedules.dialog.commandLabel")),
    ).not.toBeInTheDocument();

    // Switch to start: warnings editor disappears again.
    fireEvent.change(select, { target: { value: "start" } });
    expect(
      screen.queryByText(t("schedules.dialog.warningsLabel")),
    ).not.toBeInTheDocument();
  });

  it("creates an interval schedule with the composed request body", async () => {
    routeGet({ schedules: [] });
    mockApi.post.mockResolvedValue(schedule());
    renderTab(canFor(ALL_CODES));

    fireEvent.click(
      await screen.findByRole("button", { name: t("schedules.create") }),
    );
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.nameLabel")), {
      target: { value: "evening restart" },
    });
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.actionLabel")), {
      target: { value: "backup" },
    });
    fireEvent.change(
      screen.getByLabelText(t("schedules.dialog.intervalLabel")),
      {
        target: { value: "30" },
      },
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("schedules.dialog.create") }),
    );

    await waitFor(() => expect(mockApi.post).toHaveBeenCalledTimes(1));
    const [path, init] = mockApi.post.mock.calls[0];
    expect(path).toContain(`/communities/${CID}/servers/${SID}/schedules`);
    expect(JSON.parse(init.body)).toEqual({
      name: "evening restart",
      action: "backup",
      timezone: "UTC",
      enabled: true,
      cron: null,
      interval_seconds: 1800,
      command: null,
      warning_steps: null,
    });
  });

  it("creates a stop schedule carrying warning steps", async () => {
    routeGet({ schedules: [] });
    mockApi.post.mockResolvedValue(schedule());
    renderTab(canFor(ALL_CODES));

    fireEvent.click(
      await screen.findByRole("button", { name: t("schedules.create") }),
    );
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.nameLabel")), {
      target: { value: "nightly stop" },
    });
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.actionLabel")), {
      target: { value: "stop" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("schedules.dialog.warning.add") }),
    );
    fireEvent.change(
      screen.getByLabelText(t("schedules.dialog.warning.offset")),
      { target: { value: "5" } },
    );
    fireEvent.change(
      screen.getByLabelText(t("schedules.dialog.warning.message")),
      { target: { value: "Stopping in 5 minutes" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("schedules.dialog.create") }),
    );

    await waitFor(() => expect(mockApi.post).toHaveBeenCalledTimes(1));
    const body = JSON.parse(mockApi.post.mock.calls[0][1].body);
    expect(body.action).toBe("stop");
    expect(body.warning_steps).toEqual([
      { offset_minutes: 5, message: "Stopping in 5 minutes" },
    ]);
  });

  it("caps the warning editor at five steps", async () => {
    routeGet({ schedules: [] });
    renderTab(canFor(ALL_CODES));

    fireEvent.click(
      await screen.findByRole("button", { name: t("schedules.create") }),
    );
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.actionLabel")), {
      target: { value: "restart" },
    });
    const addButton = () =>
      screen.queryByRole("button", {
        name: t("schedules.dialog.warning.add"),
      });
    for (let i = 0; i < 5; i += 1) {
      const button = addButton();
      expect(button).not.toBeNull();
      fireEvent.click(button as HTMLElement);
    }
    // Sixth add is not offered.
    expect(addButton()).toBeNull();
  });

  it("disables the action select when editing (action is immutable)", async () => {
    routeGet();
    renderTab(canFor(ALL_CODES));

    fireEvent.click(await screen.findByText("nightly backup"));
    fireEvent.click(screen.getByRole("button", { name: t("schedules.edit") }));

    expect(
      screen.getByText(t("schedules.dialog.editTitle")),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText(t("schedules.dialog.actionLabel")),
    ).toBeDisabled();
  });

  it("maps a 422 invalid_cron reason to a cadence field error", async () => {
    routeGet({ schedules: [] });
    mockApi.post.mockRejectedValue(
      new ApiError(422, { reason: "invalid_cron" }),
    );
    renderTab(canFor(ALL_CODES));

    fireEvent.click(
      await screen.findByRole("button", { name: t("schedules.create") }),
    );
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.nameLabel")), {
      target: { value: "cronny" },
    });
    fireEvent.click(screen.getByLabelText(t("schedules.dialog.cadence.cron")));
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.cronLabel")), {
      target: { value: "not a cron" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("schedules.dialog.create") }),
    );

    expect(
      await screen.findByText(t("schedules.error.invalidCron")),
    ).toBeInTheDocument();
  });

  it("maps a 409 duplicate name to a name field error", async () => {
    routeGet({ schedules: [] });
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "schedule_name_exists" }),
    );
    renderTab(canFor(ALL_CODES));

    fireEvent.click(
      await screen.findByRole("button", { name: t("schedules.create") }),
    );
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.nameLabel")), {
      target: { value: "nightly backup" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("schedules.dialog.create") }),
    );

    expect(
      await screen.findByText(t("schedules.error.nameExists")),
    ).toBeInTheDocument();
  });

  it("rejects a sub-60-second interval client-side without a request", async () => {
    routeGet({ schedules: [] });
    renderTab(canFor(ALL_CODES));

    fireEvent.click(
      await screen.findByRole("button", { name: t("schedules.create") }),
    );
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.nameLabel")), {
      target: { value: "too fast" },
    });
    // The number input's min does not block a typed fractional value: 0.5
    // minutes is 30s, below the API's 60s floor.
    fireEvent.change(
      screen.getByLabelText(t("schedules.dialog.intervalLabel")),
      { target: { value: "0.5" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("schedules.dialog.create") }),
    );

    expect(
      await screen.findByText(t("schedules.error.intervalTooShort")),
    ).toBeInTheDocument();
    expect(mockApi.post).not.toHaveBeenCalled();
  });
});

describe("ServerSchedulesTab toggle/delete/history", () => {
  it("toggles a schedule's enabled flag via PATCH", async () => {
    routeGet();
    mockApi.patch.mockResolvedValue(schedule({ enabled: false }));
    renderTab(canFor(ALL_CODES));

    const toggle = await screen.findByLabelText(
      t("schedules.enabledLabel", { name: "nightly backup" }),
    );
    fireEvent.click(toggle);

    await waitFor(() => expect(mockApi.patch).toHaveBeenCalledTimes(1));
    const [path, init] = mockApi.patch.mock.calls[0];
    expect(path).toContain("/schedules/sch1");
    expect(JSON.parse(init.body)).toEqual({ enabled: false });
  });

  it("deletes a schedule after the confirm", async () => {
    routeGet();
    mockApi.delete.mockResolvedValue(undefined);
    renderTab(canFor(ALL_CODES));

    await screen.findByText("nightly backup");
    fireEvent.click(
      screen.getByRole("button", { name: t("schedules.delete") }),
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("schedules.deleteDialog.confirm"),
      }),
    );

    await waitFor(() => expect(mockApi.delete).toHaveBeenCalledTimes(1));
    expect(mockApi.delete.mock.calls[0][0]).toContain("/schedules/sch1");
  });

  it("shows run history with outcome, detail, and timestamps", async () => {
    routeGet({ runs: [run()] });
    renderTab(canFor(ALL_CODES));

    await screen.findByText("nightly backup");
    fireEvent.click(
      screen.getByRole("button", { name: t("schedules.history") }),
    );

    expect(
      await screen.findByText(t("schedules.runs.outcome.failure")),
    ).toBeInTheDocument();
    expect(screen.getByText("worker_unavailable")).toBeInTheDocument();
  });
});

// Route api.post by path: preview vs. create.
function routePost(
  opts: { create?: object; preview?: object; previewError?: unknown } = {},
) {
  mockApi.post.mockImplementation((path: string) => {
    if (path.includes("/preview")) {
      if (opts.previewError) return Promise.reject(opts.previewError);
      return Promise.resolve(
        opts.preview ?? {
          next_runs: [
            "2026-07-12T04:00:00Z",
            "2026-07-13T04:00:00Z",
            "2026-07-14T04:00:00Z",
            "2026-07-15T04:00:00Z",
            "2026-07-16T04:00:00Z",
          ],
        },
      );
    }
    return Promise.resolve(opts.create ?? schedule());
  });
}

describe("ServerSchedulesTab daily/weekly builder", () => {
  it("composes a daily cron expression when every-day is selected", async () => {
    routeGet({ schedules: [] });
    routePost({ create: schedule() });
    renderTab(canFor(ALL_CODES));

    fireEvent.click(
      await screen.findByRole("button", { name: t("schedules.create") }),
    );
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.nameLabel")), {
      target: { value: "daily test" },
    });
    // Switch to Daily/Weekly mode.
    fireEvent.click(
      screen.getByLabelText(t("schedules.dialog.cadence.dailyWeekly")),
    );
    // Set hour=4, minute=0 (defaults).
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.hourLabel")), {
      target: { value: "4" },
    });
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.minuteLabel")), {
      target: { value: "0" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("schedules.dialog.create") }),
    );

    await waitFor(() => {
      const postCalls = mockApi.post.mock.calls.filter(
        (call) => !(call[0] as string).includes("/preview"),
      );
      expect(postCalls.length).toBe(1);
    });
    const createCalls = mockApi.post.mock.calls.filter(
      (call) => !(call[0] as string).includes("/preview"),
    );
    const body = JSON.parse(createCalls[0][1].body);
    expect(body.cron).toBe("0 4 * * *");
    expect(body.interval_seconds).toBeNull();
  });

  it("composes a specific-days cron expression", async () => {
    routeGet({ schedules: [] });
    routePost({ create: schedule() });
    renderTab(canFor(ALL_CODES));

    fireEvent.click(
      await screen.findByRole("button", { name: t("schedules.create") }),
    );
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.nameLabel")), {
      target: { value: "weekday test" },
    });
    fireEvent.click(
      screen.getByLabelText(t("schedules.dialog.cadence.dailyWeekly")),
    );
    // Select "Specific days".
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.repeatLabel")), {
      target: { value: "specificDays" },
    });
    // Check Mon (1), Wed (3), Fri (5) via their label text.
    fireEvent.click(screen.getByLabelText(t("schedules.dialog.day.mon")));
    fireEvent.click(screen.getByLabelText(t("schedules.dialog.day.wed")));
    fireEvent.click(screen.getByLabelText(t("schedules.dialog.day.fri")));
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.hourLabel")), {
      target: { value: "18" },
    });
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.minuteLabel")), {
      target: { value: "30" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("schedules.dialog.create") }),
    );

    await waitFor(() => {
      const postCalls = mockApi.post.mock.calls.filter(
        (call) => !(call[0] as string).includes("/preview"),
      );
      expect(postCalls.length).toBe(1);
    });
    const createCalls = mockApi.post.mock.calls.filter(
      (call) => !(call[0] as string).includes("/preview"),
    );
    const body = JSON.parse(createCalls[0][1].body);
    expect(body.cron).toBe("30 18 * * 1,3,5");
  });

  it("auto-detects daily/weekly mode when editing a matching cron schedule", async () => {
    routeGet({
      schedules: [
        schedule({
          interval_seconds: null,
          cron: "30 18 * * 1,3,5",
        }),
      ],
    });
    routePost();
    renderTab(canFor(ALL_CODES));

    await screen.findByText("nightly backup");
    fireEvent.click(screen.getByRole("button", { name: t("schedules.edit") }));

    // The Daily/Weekly radio should be selected.
    const dwRadio = screen.getByLabelText(
      t("schedules.dialog.cadence.dailyWeekly"),
    );
    expect(dwRadio).toBeChecked();
    // The builder fields should be prefilled from the parsed cron.
    expect(screen.getByLabelText(t("schedules.dialog.hourLabel"))).toHaveValue(
      18,
    );
    expect(
      screen.getByLabelText(t("schedules.dialog.minuteLabel")),
    ).toHaveValue(30);
    expect(screen.getByLabelText(t("schedules.dialog.day.mon"))).toBeChecked();
    expect(screen.getByLabelText(t("schedules.dialog.day.wed"))).toBeChecked();
    expect(screen.getByLabelText(t("schedules.dialog.day.fri"))).toBeChecked();
    expect(
      screen.getByLabelText(t("schedules.dialog.day.tue")),
    ).not.toBeChecked();
  });

  it("falls back to raw cron mode for unrecognized patterns", async () => {
    routeGet({
      schedules: [
        schedule({
          interval_seconds: null,
          cron: "*/5 * * * *",
        }),
      ],
    });
    routePost();
    renderTab(canFor(ALL_CODES));

    await screen.findByText("nightly backup");
    fireEvent.click(screen.getByRole("button", { name: t("schedules.edit") }));

    // The Cron (advanced) radio should be selected.
    const cronRadio = screen.getByLabelText(t("schedules.dialog.cadence.cron"));
    expect(cronRadio).toBeChecked();
  });
});

describe("ServerSchedulesTab humanized cadence", () => {
  it("shows 'Daily at HH:MM' for a daily cron pattern", async () => {
    routeGet({
      schedules: [schedule({ interval_seconds: null, cron: "0 4 * * *" })],
    });
    renderTab(canFor(ALL_CODES));

    expect(
      await screen.findByText(
        t("schedules.cadence.dailyAt", { time: "04:00" }),
      ),
    ).toBeInTheDocument();
  });

  it("shows 'Mon, Wed, Fri at HH:MM' for a specific-days cron pattern", async () => {
    routeGet({
      schedules: [
        schedule({ interval_seconds: null, cron: "30 18 * * 1,3,5" }),
      ],
    });
    renderTab(canFor(ALL_CODES));

    const mon = t("schedules.dialog.day.mon");
    const wed = t("schedules.dialog.day.wed");
    const fri = t("schedules.dialog.day.fri");
    expect(
      await screen.findByText(
        t("schedules.cadence.daysAt", {
          days: `${mon}, ${wed}, ${fri}`,
          time: "18:30",
        }),
      ),
    ).toBeInTheDocument();
  });

  it("keeps raw cron fallback for unrecognized patterns", async () => {
    routeGet({
      schedules: [schedule({ interval_seconds: null, cron: "*/5 * * * *" })],
    });
    renderTab(canFor(ALL_CODES));

    expect(
      await screen.findByText(
        t("schedules.cadence.cron", { cron: "*/5 * * * *" }),
      ),
    ).toBeInTheDocument();
  });
});

describe("ServerSchedulesTab next-runs preview", () => {
  it("shows 5 next runs in the create dialog", async () => {
    routeGet({ schedules: [] });
    routePost();
    renderTab(canFor(ALL_CODES));

    fireEvent.click(
      await screen.findByRole("button", { name: t("schedules.create") }),
    );

    // Wait for the preview to render (debounced).
    const preview = await screen.findByTestId("next-runs-preview");
    await waitFor(() => {
      const items = within(preview).getAllByRole("listitem");
      expect(items.length).toBe(5);
    });
  });

  it("shows preview validation errors inline", async () => {
    routeGet({ schedules: [] });
    routePost({
      previewError: new ApiError(422, { reason: "invalid_cron" }),
    });
    renderTab(canFor(ALL_CODES));

    fireEvent.click(
      await screen.findByRole("button", { name: t("schedules.create") }),
    );
    // Switch to cron mode and enter an invalid expression.
    fireEvent.click(screen.getByLabelText(t("schedules.dialog.cadence.cron")));
    fireEvent.change(screen.getByLabelText(t("schedules.dialog.cronLabel")), {
      target: { value: "bad cron" },
    });

    // Wait for the debounced preview error.
    const preview = await screen.findByTestId("next-runs-preview");
    await waitFor(() => {
      expect(
        within(preview).getByText(t("schedules.error.invalidCron")),
      ).toBeInTheDocument();
    });
  });
});
