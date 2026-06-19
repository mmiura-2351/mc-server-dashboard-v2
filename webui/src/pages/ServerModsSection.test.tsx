import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import type { components } from "../api/schema";
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

function server(overrides: Record<string, unknown> = {}) {
  return {
    id: SID,
    community_id: CID,
    name: "survival",
    server_type: "fabric",
    mc_edition: "java",
    mc_version: "1.21.6",
    execution_backend: "container",
    game_port: 25565,
    desired_state: "stopped",
    observed_state: "stopped",
    observed_at: null,
    assigned_worker_id: null,
    config: {},
    slug: "survival",
    join_hostname: null,
    ...overrides,
  };
}

function mod(overrides: Record<string, unknown> = {}) {
  return {
    id: "mod-1",
    filename: "fabric-api.jar",
    display_name: "Fabric API",
    description: null,
    loader_type: "fabric",
    mod_identifier: "fabric-api",
    provides: [],
    version_number: "0.100.0",
    mc_versions: ["1.21.6"],
    side: "both",
    dependencies: [],
    sha256_hash: "deadbeef",
    sha512_hash: null,
    size_bytes: 1048576,
    source: "local",
    source_project_id: null,
    source_version_id: null,
    uploaded_by: "user-1",
    created_at: "2026-06-10T00:00:00Z",
    updated_at: "2026-06-10T00:00:00Z",
    ...overrides,
  };
}

function serverMod(overrides: Record<string, unknown> = {}) {
  return {
    assigned_at: "2026-06-15T00:00:00Z",
    assigned_by: "user-1",
    enabled: true,
    mod: mod(),
    ...overrides,
  };
}

type ModValidation = components["schemas"]["ModValidationResponse"];

const EMPTY_VALIDATION: ModValidation = {
  missing_deps: [],
  version_unsatisfied: [],
  conflicts: [],
  loader_mismatch: [],
  mc_mismatch: [],
};

// Route api.get by path: server detail, server mod set, the global library, and
// meta (consumed by the Settings tab).
type ResolutionEntry = components["schemas"]["ResolutionEntryResponse"];
type ResolutionPlan = components["schemas"]["ResolutionPlanResponse"];

// A resolution-plan entry with sane defaults; override per-status as needed.
function resolveEntry(
  overrides: Partial<ResolutionEntry> = {},
): ResolutionEntry {
  return {
    dep_identifier: "cloth-config",
    required_range: ">=10.0.0",
    status: "resolvable_from_library",
    depth: 0,
    required_by: null,
    blocked: false,
    replaces: [],
    will_import: null,
    mod: null,
    ...overrides,
  };
}

function routeGet(
  opts: {
    srv?: Record<string, unknown>;
    mods?: ReturnType<typeof serverMod>[];
    validation?: ModValidation;
    library?: ReturnType<typeof mod>[];
    plan?: ResolutionPlan;
  } = {},
) {
  const srv = server(opts.srv);
  const mods = opts.mods ?? [];
  const validation = opts.validation ?? EMPTY_VALIDATION;
  const library = opts.library ?? [];
  const plan = opts.plan ?? {
    entries: [],
    validation: EMPTY_VALIDATION,
    failed_imports: [],
  };
  mockApi.get.mockImplementation((path: string) => {
    if (path.endsWith("/resource-pack")) {
      return Promise.reject(new ApiError(404, { reason: "not_found" }));
    }
    if (path.endsWith(`/servers/${SID}/mods/resolve`)) {
      return Promise.resolve(plan);
    }
    if (path.endsWith(`/servers/${SID}/mods`)) {
      return Promise.resolve({ mods, validation });
    }
    if (path === "/api/mods") {
      return Promise.resolve({ mods: library });
    }
    if (path === "/api/meta") {
      return Promise.resolve({
        relay_enabled: false,
        default_memory_limit_mb: null,
        max_memory_limit_mb: null,
      });
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

async function openSettings() {
  renderPage();
  await screen.findByText("survival");
  fireEvent.click(
    screen.getByRole("tab", { name: t("serverDetail.tab.settings") }),
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
  restoreWs = installMockWebSocket();
});

afterEach(() => {
  restoreWs();
  vi.clearAllMocks();
});

describe("ServerModsSection — list", () => {
  it("shows the 'no mods assigned' message when the set is empty", async () => {
    routeGet({ mods: [] });
    await openSettings();

    expect(await screen.findByText(t("serverMods.none"))).toBeInTheDocument();
  });

  it("lists assigned mods with a side badge and enabled state", async () => {
    routeGet({ mods: [serverMod()] });
    await openSettings();

    expect(await screen.findByText("Fabric API")).toBeInTheDocument();
    expect(screen.getByText("0.100.0")).toBeInTheDocument();
    // Side badge for `both`.
    expect(screen.getByText(t("mods.side.both"))).toBeInTheDocument();
    // Enabled state cell.
    expect(screen.getByText(t("serverMods.enabled"))).toBeInTheDocument();
  });

  it("shows the disabled state for a disabled mod", async () => {
    routeGet({ mods: [serverMod({ enabled: false })] });
    await openSettings();

    expect(
      await screen.findByText(t("serverMods.disabled")),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: t("serverMods.enable") }),
    ).toBeInTheDocument();
  });
});

describe("ServerModsSection — validation checklist", () => {
  it("shows the 'no issues' message when validation is empty", async () => {
    routeGet({ mods: [serverMod()] });
    await openSettings();

    expect(
      await screen.findByText(t("serverMods.validation.ok")),
    ).toBeInTheDocument();
  });

  it("renders a missing-dependency finding", async () => {
    routeGet({
      mods: [serverMod()],
      validation: {
        ...EMPTY_VALIDATION,
        missing_deps: [
          {
            mod_id: "mod-1",
            depends_on: "cloth-config",
            version_range: ">=10.0.0",
          },
        ],
      },
    });
    await openSettings();

    // The mod_id resolves to the assigned mod's display name.
    expect(
      await screen.findByText(/Fabric API.*cloth-config/),
    ).toBeInTheDocument();
  });

  it("renders a version-unsatisfied finding", async () => {
    routeGet({
      mods: [serverMod()],
      validation: {
        ...EMPTY_VALIDATION,
        version_unsatisfied: [
          {
            mod_id: "mod-1",
            depends_on: "fabric-api",
            version_range: ">=0.90.0",
            present_version: "0.80.0",
          },
        ],
      },
    });
    await openSettings();

    // The mod_id resolves to the assigned mod's display name; the present
    // version and required range both surface in the finding.
    expect(
      await screen.findByText(/Fabric API.*fabric-api.*0\.90\.0.*0\.80\.0/),
    ).toBeInTheDocument();
  });

  it("renders a loader-mismatch finding", async () => {
    routeGet({
      mods: [serverMod()],
      validation: {
        ...EMPTY_VALIDATION,
        loader_mismatch: [
          { mod_id: "mod-1", mod_loader: "forge", server_loader: "fabric" },
        ],
      },
    });
    await openSettings();

    expect(
      await screen.findByText(/Fabric API.*forge.*fabric/),
    ).toBeInTheDocument();
  });

  it("renders a conflict finding", async () => {
    routeGet({
      mods: [serverMod()],
      validation: {
        ...EMPTY_VALIDATION,
        conflicts: [{ mod_id: "mod-1", conflicts_with: "optifine" }],
      },
    });
    await openSettings();

    // The mod_id resolves to the assigned mod's display name.
    expect(await screen.findByText(/Fabric API.*optifine/)).toBeInTheDocument();
  });

  it("renders an mc-mismatch finding as a warning", async () => {
    routeGet({
      mods: [serverMod()],
      validation: {
        ...EMPTY_VALIDATION,
        mc_mismatch: [
          {
            mod_id: "mod-1",
            mod_mc_versions: ["1.20.4", "1.20.6"],
            server_mc_version: "1.21.6",
          },
        ],
      },
    });
    await openSettings();

    const finding = await screen.findByText(
      /Fabric API.*1\.21\.6.*1\.20\.4, 1\.20\.6/,
    );
    expect(finding).toBeInTheDocument();
    // Warning severity (not the red `field-error`): rendered as `field-hint warn`.
    expect(finding).toHaveClass("field-hint", "warn");
  });
});

describe("ServerModsSection — assign flow", () => {
  function dialogSubmit() {
    const dialog = screen.getByRole("dialog");
    return dialog.querySelector(
      ".modal-foot .btn.primary",
    ) as HTMLButtonElement;
  }

  it("opens the dialog, multi-selects, and assigns", async () => {
    routeGet({
      mods: [],
      library: [
        mod({ id: "mod-1", display_name: "Fabric API" }),
        mod({ id: "mod-2", display_name: "Sodium" }),
      ],
    });
    mockApi.post.mockResolvedValue({ mods: [], validation: EMPTY_VALIDATION });
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", { name: t("serverMods.assign") }),
    );

    // Two checkboxes for the two compatible library mods.
    const checkboxes = await screen.findAllByRole("checkbox");
    expect(checkboxes).toHaveLength(2);

    // Submit disabled until at least one is selected.
    const submit = dialogSubmit();
    expect(submit).toBeDisabled();

    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[1]);
    expect(submit).not.toBeDisabled();

    fireEvent.click(submit);

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/mods`,
        { body: JSON.stringify({ mod_ids: ["mod-1", "mod-2"] }) },
      ),
    );
  });

  it("filters the library to loader-compatible mods and excludes assigned mods", async () => {
    // A fabric server runs fabric + quilt mods (LOADER_COMPAT mirrors the
    // backend validation map): quilt is offered, forge is not.
    routeGet({
      mods: [serverMod({ mod: mod({ id: "mod-1" }) })],
      library: [
        mod({ id: "mod-1", display_name: "Fabric API" }), // already assigned
        mod({ id: "mod-2", display_name: "Forge Mod", loader_type: "forge" }), // incompatible loader
        mod({ id: "mod-3", display_name: "Sodium" }), // assignable (fabric)
        mod({
          id: "mod-4",
          display_name: "Quilt Mod",
          loader_type: "quilt",
        }), // assignable (quilt compatible with fabric)
      ],
    });
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", { name: t("serverMods.assign") }),
    );

    const checkboxes = await screen.findAllByRole("checkbox");
    expect(checkboxes).toHaveLength(2);
    expect(screen.getByText(/Sodium/)).toBeInTheDocument();
    expect(screen.getByText(/Quilt Mod/)).toBeInTheDocument();
    expect(screen.queryByText(/Forge Mod/)).not.toBeInTheDocument();
  });

  it("shows the empty message when no compatible mods remain", async () => {
    routeGet({ mods: [], library: [] });
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", { name: t("serverMods.assign") }),
    );

    expect(
      await screen.findByText(t("serverMods.assignDialog.empty")),
    ).toBeInTheDocument();
  });

  it("surfaces a 409 server_unsettled error on assign", async () => {
    routeGet({
      mods: [],
      library: [mod({ id: "mod-1" })],
    });
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "server_unsettled" }),
    );
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", { name: t("serverMods.assign") }),
    );
    const checkbox = await screen.findByRole("checkbox");
    fireEvent.click(checkbox);
    fireEvent.click(dialogSubmit());

    expect(
      await screen.findByText(t("serverDetail.error.unsettled")),
    ).toBeInTheDocument();
  });
});

describe("ServerModsSection — unassign and toggle", () => {
  it("unassigns a mod", async () => {
    routeGet({ mods: [serverMod()] });
    mockApi.delete.mockResolvedValue(undefined);
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", { name: t("serverMods.unassign") }),
    );

    await waitFor(() =>
      expect(mockApi.delete).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/mods/mod-1`,
      ),
    );
  });

  it("disables an enabled mod via the disable endpoint", async () => {
    routeGet({ mods: [serverMod({ enabled: true })] });
    mockApi.post.mockResolvedValue(undefined);
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", { name: t("serverMods.disable") }),
    );

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/mods/mod-1/disable`,
      ),
    );
  });

  it("enables a disabled mod via the enable endpoint", async () => {
    routeGet({ mods: [serverMod({ enabled: false })] });
    mockApi.post.mockResolvedValue(undefined);
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", { name: t("serverMods.enable") }),
    );

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/mods/mod-1/enable`,
      ),
    );
  });

  it("surfaces a 409 server_unsettled error on unassign", async () => {
    routeGet({ mods: [serverMod()] });
    mockApi.delete.mockRejectedValue(
      new ApiError(409, { reason: "server_unsettled" }),
    );
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", { name: t("serverMods.unassign") }),
    );

    expect(
      await screen.findByText(t("serverDetail.error.unsettled")),
    ).toBeInTheDocument();
  });
});

describe("ServerModsSection — at-rest and permission gating", () => {
  it("disables mutating controls when the server is running", async () => {
    routeGet({
      srv: { observed_state: "running", desired_state: "running" },
      mods: [serverMod()],
    });
    await openSettings();

    expect(
      await screen.findByRole("button", { name: t("serverMods.assign") }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: t("serverMods.disable") }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: t("serverMods.unassign") }),
    ).toBeDisabled();
    expect(screen.getByText(t("serverMods.notAtRest"))).toBeInTheDocument();
  });

  it("keeps the client modpack download enabled while running", async () => {
    routeGet({
      srv: { observed_state: "running", desired_state: "running" },
      mods: [serverMod()],
    });
    await openSettings();

    expect(
      await screen.findByRole("button", {
        name: t("serverMods.downloadClient"),
      }),
    ).not.toBeDisabled();
  });

  it("hides mutating controls without server:update", async () => {
    mockCan = (code) => code !== "server:update";
    routeGet({ mods: [serverMod()] });
    await openSettings();

    await screen.findByText("Fabric API");
    expect(
      screen.queryByRole("button", { name: t("serverMods.assign") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("serverMods.disable") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("serverMods.unassign") }),
    ).not.toBeInTheDocument();
    // Reads stay: the download button remains visible.
    expect(
      screen.getByRole("button", { name: t("serverMods.downloadClient") }),
    ).toBeInTheDocument();
  });
});

describe("ServerModsSection — client modpack download", () => {
  it("triggers a download of the client modpack zip", async () => {
    routeGet({ mods: [serverMod()] });
    mockDownload.downloadFile.mockResolvedValue(undefined);
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("serverMods.downloadClient"),
      }),
    );

    await waitFor(() =>
      expect(mockDownload.downloadFile).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/client-mods/download`,
        "survival-client-mods.zip",
      ),
    );
  });
});

describe("ServerModsSection — resolve dependencies", () => {
  async function openResolve() {
    fireEvent.click(
      await screen.findByRole("button", {
        name: t("serverMods.resolve.action"),
      }),
    );
    return screen.findByRole("dialog");
  }

  it("renders the plan grouped by library add, Modrinth import, blocked, and unresolvable", async () => {
    routeGet({
      mods: [serverMod()],
      plan: {
        failed_imports: [],
        validation: EMPTY_VALIDATION,
        entries: [
          // Library add with an upgrade replacement.
          resolveEntry({
            dep_identifier: "fabric-api",
            status: "resolvable_from_library",
            mod: mod({
              id: "lib-1",
              display_name: "Fabric API",
              version_number: "0.110.0",
            }),
            replaces: [mod({ id: "old-1", display_name: "Fabric API 0.80" })],
          }),
          // Transitive Modrinth import.
          resolveEntry({
            dep_identifier: "cloth-config",
            status: "needs_import",
            depth: 1,
            required_by: "fabric-api",
            will_import: {
              project_id: "p1",
              version_id: "v1",
              slug: "cloth-config",
              version_number: "11.0.0",
            },
          }),
          // Blocked by a conflict.
          resolveEntry({
            dep_identifier: "conflicty",
            status: "resolvable_from_library",
            blocked: true,
            mod: mod({ id: "lib-2", display_name: "Conflicty" }),
          }),
          // Unresolvable.
          resolveEntry({
            dep_identifier: "mystery-lib",
            status: "unresolvable",
          }),
        ],
      },
    });
    await openSettings();
    await openResolve();

    expect(
      await screen.findByText(t("serverMods.resolve.group.library")),
    ).toBeInTheDocument();
    expect(
      screen.getByText(t("serverMods.resolve.group.import")),
    ).toBeInTheDocument();
    expect(
      screen.getByText(t("serverMods.resolve.group.blocked")),
    ).toBeInTheDocument();
    expect(
      screen.getByText(t("serverMods.resolve.group.unresolvable")),
    ).toBeInTheDocument();

    // Library upgrade detail: "upgrades X → Y".
    expect(
      screen.getByText(/Fabric API 0\.80.*Fabric API \(0\.110\.0\)/),
    ).toBeInTheDocument();
    // Modrinth import target rendered as project @ version, with required-by.
    expect(screen.getByText(/cloth-config @ 11\.0\.0/)).toBeInTheDocument();
    expect(screen.getByText(/required by.*fabric-api/)).toBeInTheDocument();
  });

  it("shows the chosen library mod for a plain (non-upgrade) add", async () => {
    routeGet({
      mods: [serverMod()],
      plan: {
        failed_imports: [],
        validation: EMPTY_VALIDATION,
        entries: [
          resolveEntry({
            dep_identifier: "sodium",
            status: "resolvable_from_library",
            mod: mod({
              id: "lib-9",
              display_name: "Sodium",
              version_number: "0.6.0",
            }),
          }),
        ],
      },
    });
    await openSettings();
    await openResolve();

    expect(await screen.findByText(/Sodium \(0\.6\.0\)/)).toBeInTheDocument();
  });

  it("shows the empty message and no apply button when everything is already satisfied", async () => {
    routeGet({
      mods: [serverMod()],
      plan: {
        failed_imports: [],
        validation: EMPTY_VALIDATION,
        entries: [resolveEntry({ status: "already_satisfied" })],
      },
    });
    await openSettings();
    await openResolve();

    expect(
      await screen.findByText(t("serverMods.resolve.nothing")),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("serverMods.resolve.apply") }),
    ).not.toBeInTheDocument();
  });

  it("applies the plan, refreshes the mod set, and closes", async () => {
    routeGet({
      mods: [serverMod()],
      plan: {
        failed_imports: [],
        validation: EMPTY_VALIDATION,
        entries: [resolveEntry({ status: "resolvable_from_library" })],
      },
    });
    mockApi.post.mockResolvedValue({
      entries: [],
      validation: EMPTY_VALIDATION,
      failed_imports: [],
    });
    await openSettings();
    await openResolve();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("serverMods.resolve.apply"),
      }),
    );

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/mods/resolve`,
      ),
    );
    // Success toast + dialog dismissed.
    expect(
      await screen.findByText(t("serverMods.resolve.applied")),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    // The mod set is re-fetched after apply (initial detail load + refresh).
    await waitFor(() => {
      const modsGets = mockApi.get.mock.calls.filter(
        (c) => c[0] === `/api/communities/${CID}/servers/${SID}/mods`,
      );
      expect(modsGets.length).toBeGreaterThan(1);
    });
  });

  it("surfaces failed imports after applying", async () => {
    routeGet({
      mods: [serverMod()],
      plan: {
        failed_imports: [],
        validation: EMPTY_VALIDATION,
        entries: [
          resolveEntry({
            status: "needs_import",
            will_import: {
              project_id: "p1",
              version_id: "v1",
              slug: "cloth-config",
              version_number: "11.0.0",
            },
          }),
        ],
      },
    });
    mockApi.post.mockResolvedValue({
      entries: [],
      validation: EMPTY_VALIDATION,
      failed_imports: ["cloth-config"],
    });
    await openSettings();
    await openResolve();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("serverMods.resolve.apply"),
      }),
    );

    expect(
      await screen.findByText(
        t("serverMods.resolve.failedImports").replace("{deps}", "cloth-config"),
      ),
    ).toBeInTheDocument();
  });

  it("surfaces a 409 server_unsettled error on apply", async () => {
    routeGet({
      mods: [serverMod()],
      plan: {
        failed_imports: [],
        validation: EMPTY_VALIDATION,
        entries: [resolveEntry({ status: "resolvable_from_library" })],
      },
    });
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "server_unsettled" }),
    );
    await openSettings();
    await openResolve();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("serverMods.resolve.apply"),
      }),
    );

    expect(
      await screen.findByText(t("serverDetail.error.unsettled")),
    ).toBeInTheDocument();
  });

  it("disables apply while the server is running but still shows the plan", async () => {
    routeGet({
      srv: { observed_state: "running", desired_state: "running" },
      mods: [serverMod()],
      plan: {
        failed_imports: [],
        validation: EMPTY_VALIDATION,
        entries: [resolveEntry({ status: "resolvable_from_library" })],
      },
    });
    await openSettings();
    await openResolve();

    // The read-only plan still renders while running.
    expect(
      await screen.findByText(t("serverMods.resolve.group.library")),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: t("serverMods.resolve.apply") }),
    ).toBeDisabled();
    expect(
      screen.getByText(t("serverMods.resolve.notAtRest")),
    ).toBeInTheDocument();
  });

  it("hides the resolve action without server:update", async () => {
    mockCan = (code) => code !== "server:update";
    routeGet({ mods: [serverMod()] });
    await openSettings();

    await screen.findByText("Fabric API");
    expect(
      screen.queryByRole("button", { name: t("serverMods.resolve.action") }),
    ).not.toBeInTheDocument();
  });

  it("surfaces a plan load error", async () => {
    routeGet({ mods: [serverMod()] });
    mockApi.get.mockImplementation((path: string) => {
      if (path.endsWith("/resource-pack")) {
        return Promise.reject(new ApiError(404, { reason: "not_found" }));
      }
      if (path.endsWith(`/servers/${SID}/mods/resolve`)) {
        return Promise.reject(new ApiError(500, {}));
      }
      if (path.endsWith(`/servers/${SID}/mods`)) {
        return Promise.resolve({
          mods: [serverMod()],
          validation: EMPTY_VALIDATION,
        });
      }
      if (path === "/api/meta") {
        return Promise.resolve({
          relay_enabled: false,
          default_memory_limit_mb: null,
          max_memory_limit_mb: null,
        });
      }
      return Promise.resolve(server());
    });
    await openSettings();
    await openResolve();

    expect(
      await screen.findByText(t("serverMods.resolve.loadError")),
    ).toBeInTheDocument();
  });
});
