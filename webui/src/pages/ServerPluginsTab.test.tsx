/**
 * Tests for the dependency/compatibility validation checklist (issue #1307).
 *
 * Renders the plugins tab with a mocked API returning installed plugins and a
 * validation payload, and asserts the checklist surfaces each finding kind (or
 * the all-clear message when none).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import type { components } from "../api/schema";
import { ToastProvider } from "../components/Toast.tsx";
import type { Can } from "../permissions/useCan.ts";
import { ServerPluginsTab } from "./ServerPluginsTab.tsx";

type PluginValidationResponse =
  components["schemas"]["PluginValidationResponse"];

const CID = "c1";
const SID = "s1";

const mockApi = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
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

const mockDownload = vi.hoisted(() => ({ downloadFile: vi.fn() }));

vi.mock("../api/download.ts", () => mockDownload);

vi.mock("../permissions/ActiveCommunityProvider.tsx", () => ({
  useActiveCommunity: () => ({
    communityId: CID,
    setCommunityId: vi.fn(),
    communities: [{ id: CID, name: "Sakura" }],
  }),
}));

// biome-ignore lint/suspicious/noExplicitAny: minimal server fixture for the tab.
function server(overrides: Record<string, unknown> = {}): any {
  return {
    id: SID,
    community_id: CID,
    name: "srv",
    server_type: "fabric",
    mc_edition: "java",
    mc_version: "1.21",
    desired_state: "stopped",
    observed_state: "stopped",
    ...overrides,
  };
}

const allow: Can = () => true;

const EMPTY_VALIDATION: PluginValidationResponse = {
  missing_deps: [],
  missing_catalog_deps: [],
  version_unsatisfied: [],
  conflicts: [],
  mc_mismatch: [],
};

function plugin(overrides: Record<string, unknown> = {}) {
  return {
    id: "p1",
    server_id: SID,
    rel_path: "mods/sodium.jar",
    filename: "sodium.jar",
    display_name: "Sodium",
    description: null,
    loader_type: "mod",
    source: "local",
    source_project_id: null,
    source_version_id: null,
    version_number: "0.5.0",
    checksum_sha512: null,
    size_bytes: 100,
    enabled: true,
    installed_by: null,
    created_at: "2026-06-20T00:00:00Z",
    updated_at: "2026-06-20T00:00:00Z",
    mod_identifier: "sodium",
    side: "both",
    ...overrides,
  };
}

function renderTab() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <ServerPluginsTab server={server()} communityId={CID} can={allow} />
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { ...result, queryClient };
}

/** Route the tab's GET calls by URL suffix. */
function mockGets({
  plugins,
  validation,
}: {
  plugins: unknown[];
  validation: PluginValidationResponse;
}) {
  mockApi.get.mockImplementation((url: string) => {
    if (url.endsWith("/plugins/validate")) {
      return Promise.resolve(validation);
    }
    if (url.endsWith("/plugins/updates")) {
      return Promise.resolve({ updates: [] });
    }
    if (url.endsWith("/plugins")) {
      return Promise.resolve({ plugins });
    }
    return Promise.resolve({});
  });
}

describe("ServerPluginsTab validation checklist", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows the all-clear message when there are no findings", async () => {
    mockGets({ plugins: [plugin()], validation: EMPTY_VALIDATION });
    renderTab();
    await waitFor(() => {
      expect(screen.getByText("No issues found.")).toBeInTheDocument();
    });
  });

  it("reports a missing required dependency by display name", async () => {
    mockGets({
      plugins: [plugin()],
      validation: {
        ...EMPTY_VALIDATION,
        missing_deps: [
          {
            mod_id: "sodium",
            depends_on: "fabric-api",
            version_range: ">=0.90.0",
          },
        ],
      },
    });
    renderTab();
    await waitFor(() => {
      expect(
        screen.getByText(/Sodium requires fabric-api/),
      ).toBeInTheDocument();
    });
  });

  it("reports a missing Modrinth catalog dependency by its title", async () => {
    mockGets({
      plugins: [
        plugin({ display_name: "Roughly Enough Items", mod_identifier: "rei" }),
      ],
      validation: {
        ...EMPTY_VALIDATION,
        missing_catalog_deps: [
          {
            mod_id: "rei",
            project_id: "lhGA9TYQ",
            slug: "architectury-api",
            title: "Architectury",
          },
        ],
      },
    });
    renderTab();
    await waitFor(() => {
      expect(
        screen.getByText(/Roughly Enough Items requires Architectury/),
      ).toBeInTheDocument();
    });
  });

  it("falls back to the project id when no catalog label was captured", async () => {
    mockGets({
      plugins: [plugin({ mod_identifier: "rei" })],
      validation: {
        ...EMPTY_VALIDATION,
        missing_catalog_deps: [
          {
            mod_id: "rei",
            project_id: "lhGA9TYQ",
            slug: null,
            title: null,
          },
        ],
      },
    });
    renderTab();
    await waitFor(() => {
      expect(screen.getByText(/requires lhGA9TYQ/)).toBeInTheDocument();
    });
  });

  it("reports an MC-version mismatch", async () => {
    mockGets({
      plugins: [plugin()],
      validation: {
        ...EMPTY_VALIDATION,
        mc_mismatch: [
          {
            mod_id: "sodium",
            mod_mc_versions: ["1.20.4"],
            server_mc_version: "1.21",
          },
        ],
      },
    });
    renderTab();
    await waitFor(() => {
      expect(
        screen.getByText(/Sodium does not list MC 1.21/),
      ).toBeInTheDocument();
    });
  });

  it("omits parentheses when version_range is empty (#1339)", async () => {
    mockGets({
      plugins: [plugin()],
      validation: {
        ...EMPTY_VALIDATION,
        missing_deps: [
          {
            mod_id: "sodium",
            depends_on: "Vault",
            version_range: "",
          },
        ],
      },
    });
    renderTab();
    await waitFor(() => {
      const el = screen.getByText(/Sodium requires Vault/);
      expect(el).toBeInTheDocument();
      // Must NOT contain empty parentheses.
      expect(el.textContent).not.toContain("()");
      // Must render without any parenthesized range portion.
      expect(el.textContent).toBe(
        "Sodium requires Vault, which is not installed.",
      );
    });
  });

  it("still shows the range in parentheses when version_range is set (#1339)", async () => {
    mockGets({
      plugins: [plugin()],
      validation: {
        ...EMPTY_VALIDATION,
        missing_deps: [
          {
            mod_id: "sodium",
            depends_on: "fabric-api",
            version_range: ">=0.90.0",
          },
        ],
      },
    });
    renderTab();
    await waitFor(() => {
      expect(
        screen.getByText(
          "Sodium requires fabric-api (>=0.90.0), which is not installed.",
        ),
      ).toBeInTheDocument();
    });
  });

  it("does not render the checklist when no plugins are installed", async () => {
    mockGets({ plugins: [], validation: EMPTY_VALIDATION });
    renderTab();
    await waitFor(() => {
      // The fabric fixture manages mods, so the empty-state names "mods" (#1320).
      expect(screen.getByText("No mods installed.")).toBeInTheDocument();
    });
    expect(
      screen.queryByText("Dependencies & compatibility"),
    ).not.toBeInTheDocument();
  });

  it("renders $-patterns in plugin names literally (#1406)", async () => {
    mockGets({
      plugins: [
        plugin({ display_name: "Cash$&Money", mod_identifier: "cashmod" }),
      ],
      validation: {
        ...EMPTY_VALIDATION,
        missing_deps: [
          {
            mod_id: "cashmod",
            depends_on: "fabric-api",
            version_range: "",
          },
        ],
      },
    });
    renderTab();
    await waitFor(() => {
      expect(
        screen.getByText(/Cash\$&Money requires fabric-api/),
      ).toBeInTheDocument();
    });
  });
});

describe("ServerPluginsTab loader-aware noun (#1320)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  function renderTabFor(serverType: string) {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <ToastProvider>
            <ServerPluginsTab
              server={server({ server_type: serverType })}
              communityId={CID}
              can={allow}
            />
          </ToastProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
  }

  it("names the empty-state 'mods' for a fabric server", async () => {
    mockGets({ plugins: [], validation: EMPTY_VALIDATION });
    renderTabFor("fabric");
    await waitFor(() => {
      expect(screen.getByText("No mods installed.")).toBeInTheDocument();
    });
  });

  it("names the empty-state 'mods' for a forge server", async () => {
    mockGets({ plugins: [], validation: EMPTY_VALIDATION });
    renderTabFor("forge");
    await waitFor(() => {
      expect(screen.getByText("No mods installed.")).toBeInTheDocument();
    });
  });

  it("names the empty-state 'plugins' for a paper server", async () => {
    mockGets({ plugins: [], validation: EMPTY_VALIDATION });
    renderTabFor("paper");
    await waitFor(() => {
      expect(screen.getByText("No plugins installed.")).toBeInTheDocument();
    });
  });
});

describe("ServerPluginsTab action-button alignment (#1320)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  /** A minimal latest_version payload to mark a plugin as update-available. */
  const LATEST_VERSION = {
    date_published: "2026-06-20T00:00:00Z",
    dependencies: [],
    files: [],
    game_versions: ["1.21"],
    loaders: ["fabric"],
    name: "Sodium 0.6.0",
    version_id: "v-new",
    version_number: "0.6.0",
  };

  // One plugin has an update available, the other does not. The update row
  // renders a real "Update" button; the other reserves an inert placeholder so
  // the always-present actions stay column-aligned across rows.
  function mockGetsWithUpdate() {
    const updated = plugin({ id: "p1", display_name: "Sodium" });
    mockApi.get.mockImplementation((url: string) => {
      if (url.endsWith("/plugins/validate")) {
        return Promise.resolve(EMPTY_VALIDATION);
      }
      if (url.endsWith("/plugins/updates")) {
        return Promise.resolve({
          updates: [{ plugin: updated, latest_version: LATEST_VERSION }],
        });
      }
      if (url.endsWith("/plugins")) {
        return Promise.resolve({
          plugins: [updated, plugin({ id: "p2", display_name: "Lithium" })],
        });
      }
      return Promise.resolve({});
    });
  }

  it("keeps the action column aligned: real Update button on the update row, an inert placeholder otherwise", async () => {
    mockGetsWithUpdate();
    renderTab();

    await waitFor(() => {
      expect(screen.getByText("Lithium")).toBeInTheDocument();
    });

    // The update-available row renders a real, clickable Update button.
    const updateButtons = screen
      .getAllByText("Update")
      .filter((el) => el.tagName === "BUTTON");
    expect(updateButtons).toHaveLength(1);

    // The non-update row reserves an inert placeholder of the same width so the
    // following Remove button stays column-aligned (not a clickable button).
    const updatePlaceholders = screen
      .getAllByText("Update")
      .filter(
        (el) =>
          el.tagName === "SPAN" && el.classList.contains("row-actions-spacer"),
      );
    expect(updatePlaceholders).toHaveLength(1);
  });
});

describe("ServerPluginsTab side + client modpack (issue #1308)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders a side override control reflecting the plugin's side", async () => {
    mockGets({
      plugins: [plugin({ side: "client" })],
      validation: EMPTY_VALIDATION,
    });
    renderTab();
    const select = await screen.findByLabelText<HTMLSelectElement>("Runs on");
    expect(select.value).toBe("client");
  });

  it("posts the side override on change", async () => {
    mockApi.post.mockResolvedValue({});
    mockGets({ plugins: [plugin()], validation: EMPTY_VALIDATION });
    renderTab();
    const select = await screen.findByLabelText<HTMLSelectElement>("Runs on");
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(select, { target: { value: "client" } });
    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        expect.stringContaining("/plugins/p1/side"),
        { body: JSON.stringify({ side: "client" }) },
      );
    });
  });

  it("shows the download button when client mods exist and downloads", async () => {
    mockDownload.downloadFile.mockResolvedValue(undefined);
    mockGets({
      plugins: [plugin({ side: "client" })],
      validation: EMPTY_VALIDATION,
    });
    renderTab();
    const button = await screen.findByText("Download client modpack");
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.click(button);
    await waitFor(() => {
      expect(mockDownload.downloadFile).toHaveBeenCalledWith(
        expect.stringContaining("/client-mods/download"),
        "mods.zip",
      );
    });
  });

  it("hides the download button when no client mods exist", async () => {
    mockGets({
      plugins: [plugin({ side: "server" })],
      validation: EMPTY_VALIDATION,
    });
    renderTab();
    await waitFor(() => {
      expect(screen.getByText("Sodium")).toBeInTheDocument();
    });
    expect(
      screen.queryByText("Download client modpack"),
    ).not.toBeInTheDocument();
  });
});

describe("ServerPluginsTab dependency resolution (issue #1309)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const RESOLVE_PLAN = {
    entries: [
      {
        dep_identifier: "fabric-api",
        required_range: ">=0.90.0",
        status: "needs_import",
        will_import: {
          project_id: "P_FABRICAPI",
          version_id: "V1",
          slug: "fabric-api",
          version_number: "0.92.0",
        },
        depth: 0,
        required_by: null,
        blocked: false,
      },
    ],
    validation: EMPTY_VALIDATION,
  };

  it("shows the planned imports and applies on confirm", async () => {
    mockGets({ plugins: [plugin()], validation: EMPTY_VALIDATION });
    mockApi.post.mockImplementation((url: string) => {
      if (url.endsWith("/plugins/resolve/apply")) {
        return Promise.resolve({
          plan: RESOLVE_PLAN,
          installed: [],
          failed: [],
        });
      }
      if (url.endsWith("/plugins/resolve")) {
        return Promise.resolve(RESOLVE_PLAN);
      }
      return Promise.resolve({});
    });
    renderTab();

    await waitFor(() => {
      expect(screen.getByText("Sodium")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Resolve dependencies"));

    // The plan modal lists the dep that will be imported from Modrinth.
    await waitFor(() => {
      expect(
        screen.getByText(/fabric-api → fabric-api 0.92.0/),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Install dependencies"));
    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        expect.stringContaining("/plugins/resolve/apply"),
      );
    });
  });

  it("surfaces a blocked conflict and disables apply", async () => {
    mockGets({ plugins: [plugin()], validation: EMPTY_VALIDATION });
    mockApi.post.mockImplementation((url: string) => {
      if (url.endsWith("/plugins/resolve")) {
        return Promise.resolve({
          entries: [
            {
              dep_identifier: "fabric-api",
              required_range: "",
              status: "needs_import",
              will_import: {
                project_id: "P",
                version_id: "V1",
                slug: "fabric-api",
                version_number: "0.92.0",
              },
              depth: 0,
              required_by: null,
              blocked: true,
            },
          ],
          validation: EMPTY_VALIDATION,
        });
      }
      return Promise.resolve({});
    });
    renderTab();

    await waitFor(() => {
      expect(screen.getByText("Sodium")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Resolve dependencies"));

    await waitFor(() => {
      expect(screen.getByText("Blocked by conflicts")).toBeInTheDocument();
    });
    // No importable deps -> the apply button is disabled.
    expect(
      screen.getByText("Install dependencies").closest("button"),
    ).toBeDisabled();
  });

  it("labels a catalog-incompatible block with the project slug", async () => {
    // A catalog-incompatible block (issue #1318) carries a project_id as its
    // dep_identifier; the readable label comes from will_import.slug.
    mockGets({ plugins: [plugin()], validation: EMPTY_VALIDATION });
    mockApi.post.mockImplementation((url: string) => {
      if (url.endsWith("/plugins/resolve")) {
        return Promise.resolve({
          entries: [
            {
              dep_identifier: "ARCHPROJECTID",
              required_range: "",
              status: "needs_import",
              will_import: {
                project_id: "ARCHPROJECTID",
                version_id: "V1",
                slug: "architectury",
                version_number: "9.0.0",
              },
              depth: 0,
              required_by: null,
              blocked: true,
            },
          ],
          validation: EMPTY_VALIDATION,
        });
      }
      return Promise.resolve({});
    });
    renderTab();

    await waitFor(() => {
      expect(screen.getByText("Sodium")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Resolve dependencies"));

    await waitFor(() => {
      expect(
        screen.getByText(/architectury cannot be installed/),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText(/ARCHPROJECTID/)).not.toBeInTheDocument();
  });
});

describe("ServerPluginsTab Paper: no side column, no download button (issue #1342)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  function renderTabFor(serverType: string) {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <ToastProvider>
            <ServerPluginsTab
              server={server({ server_type: serverType })}
              communityId={CID}
              can={allow}
            />
          </ToastProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
  }

  it("hides the side column on a paper server", async () => {
    mockGets({
      plugins: [plugin({ side: "server" })],
      validation: EMPTY_VALIDATION,
    });
    renderTabFor("paper");
    await waitFor(() => {
      expect(screen.getByText("Sodium")).toBeInTheDocument();
    });
    // The Side column header should not be rendered.
    expect(
      screen.queryByText("Runs on", { selector: "th" }),
    ).not.toBeInTheDocument();
    // The Side select should not be rendered.
    expect(screen.queryByLabelText("Runs on")).not.toBeInTheDocument();
  });

  it("hides the download button on a paper server even with client-side plugins", async () => {
    mockGets({
      plugins: [plugin({ side: "client" })],
      validation: EMPTY_VALIDATION,
    });
    renderTabFor("paper");
    await waitFor(() => {
      expect(screen.getByText("Sodium")).toBeInTheDocument();
    });
    expect(
      screen.queryByText("Download client modpack"),
    ).not.toBeInTheDocument();
  });

  it("shows the side column on a fabric server", async () => {
    mockGets({
      plugins: [plugin({ side: "both" })],
      validation: EMPTY_VALIDATION,
    });
    renderTabFor("fabric");
    const select = await screen.findByLabelText<HTMLSelectElement>("Runs on");
    expect(select).toBeInTheDocument();
  });

  it("shows the download button on a fabric server with client mods", async () => {
    mockDownload.downloadFile.mockResolvedValue(undefined);
    mockGets({
      plugins: [plugin({ side: "client" })],
      validation: EMPTY_VALIDATION,
    });
    renderTabFor("fabric");
    const button = await screen.findByText("Download client modpack");
    expect(button).toBeInTheDocument();
  });
});

describe("ServerPluginsTab error messages (issue #1345)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  /** Trigger the file upload mutation with a failing ApiError. */
  async function triggerUploadError(reason: string) {
    mockGets({ plugins: [plugin()], validation: EMPTY_VALIDATION });
    mockPostFormWithProgress.mockRejectedValue(new ApiError(409, { reason }));
    renderTab();
    await waitFor(() => {
      expect(screen.getByText("Sodium")).toBeInTheDocument();
    });
    // Click the Upload JAR button to open the file picker, then simulate a
    // file selection via the hidden input.
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    const file = new File(["x"], "test.jar", {
      type: "application/java-archive",
    });
    fireEvent.change(input, { target: { files: [file] } });
  }

  it("shows a specific message for plugin_already_exists", async () => {
    await triggerUploadError("plugin_already_exists");
    await waitFor(() => {
      expect(
        screen.getByText(
          "A mod with the same name or project is already installed.",
        ),
      ).toBeInTheDocument();
    });
  });

  it("shows a specific message for server_unsettled", async () => {
    await triggerUploadError("server_unsettled");
    await waitFor(() => {
      expect(
        screen.getByText(
          "The server is not ready. Wait for the current operation to finish.",
        ),
      ).toBeInTheDocument();
    });
  });

  it("shows a specific message for catalog_unavailable", async () => {
    await triggerUploadError("catalog_unavailable");
    await waitFor(() => {
      expect(
        screen.getByText("Could not reach Modrinth. Please try again later."),
      ).toBeInTheDocument();
    });
  });

  it("shows a specific message for invalid_path", async () => {
    await triggerUploadError("invalid_path");
    await waitFor(() => {
      expect(
        screen.getByText(
          "Invalid file. Only .jar files can be uploaded as mods.",
        ),
      ).toBeInTheDocument();
    });
  });

  it("shows a specific message for file_too_large", async () => {
    await triggerUploadError("file_too_large");
    await waitFor(() => {
      expect(
        screen.getByText(
          "The file is too large. Maximum upload size is 512 MB.",
        ),
      ).toBeInTheDocument();
    });
  });

  it("falls back to the generic message for an unknown reason", async () => {
    await triggerUploadError("some_unknown_reason");
    await waitFor(() => {
      expect(
        screen.getByText("Something went wrong. Please try again."),
      ).toBeInTheDocument();
    });
  });
});

describe("Modrinth modal: popular mods on initial open (issue #1351)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("fires a search with empty query on open and shows popular results", async () => {
    mockGets({ plugins: [plugin()], validation: EMPTY_VALIDATION });
    // The empty-query catalog search returns popular mods.
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes("/catalog/search")) {
        return Promise.resolve({
          hits: [
            {
              project_id: "PROJ1",
              slug: "sodium",
              title: "Sodium",
              description: "Rendering engine",
              author: "CaffeineMC",
              downloads: 50000,
              icon_url: null,
              categories: [],
              latest_game_versions: ["1.21"],
            },
          ],
          limit: 20,
          offset: 0,
          total_hits: 1,
        });
      }
      if (url.endsWith("/plugins/validate")) {
        return Promise.resolve(EMPTY_VALIDATION);
      }
      if (url.endsWith("/plugins/updates")) {
        return Promise.resolve({ updates: [] });
      }
      if (url.endsWith("/plugins")) {
        return Promise.resolve({ plugins: [plugin()] });
      }
      return Promise.resolve({});
    });
    renderTab();

    await waitFor(() => {
      expect(screen.getByText("Sodium")).toBeInTheDocument();
    });

    // Open the Modrinth browse modal.
    fireEvent.click(screen.getByText("Browse Modrinth"));

    // The popular results should appear without typing anything.
    await waitFor(() => {
      // The search result hit shows the title "Sodium" in the search results.
      const hits = screen.getAllByText("Sodium");
      // At least one is in the search results area (the others are in the
      // installed plugins table).
      expect(hits.length).toBeGreaterThanOrEqual(2);
    });
  });
});

describe("Modrinth modal: installed version indicator (issue #1350)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  /** Helper to open the browse modal and select a project. */
  function mockBrowseAndDetail({
    plugins: pluginsList,
    projectId,
    versions,
  }: {
    plugins: ReturnType<typeof plugin>[];
    projectId: string;
    versions: {
      version_id: string;
      version_number: string;
      name: string;
      game_versions: string[];
    }[];
  }) {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes("/catalog/projects/")) {
        return Promise.resolve({
          project: {
            project_id: projectId,
            slug: "sodium",
            title: "Sodium",
            description: "Rendering engine",
            author: "CaffeineMC",
            downloads: 50000,
            icon_url: null,
            categories: [],
            game_versions: ["1.21"],
            loaders: ["fabric"],
            body: "",
          },
          versions: versions.map((v) => ({
            ...v,
            date_published: "2026-06-20T00:00:00Z",
            dependencies: [],
            files: [],
            loaders: ["fabric"],
          })),
        });
      }
      if (url.includes("/catalog/search")) {
        return Promise.resolve({
          hits: [
            {
              project_id: projectId,
              slug: "sodium",
              title: "Sodium",
              description: "Rendering engine",
              author: "CaffeineMC",
              downloads: 50000,
              icon_url: null,
              categories: [],
              latest_game_versions: ["1.21"],
            },
          ],
          limit: 20,
          offset: 0,
          total_hits: 1,
        });
      }
      if (url.endsWith("/plugins/validate")) {
        return Promise.resolve(EMPTY_VALIDATION);
      }
      if (url.endsWith("/plugins/updates")) {
        return Promise.resolve({ updates: [] });
      }
      if (url.endsWith("/plugins")) {
        return Promise.resolve({ plugins: pluginsList });
      }
      return Promise.resolve({});
    });
  }

  it("shows 'Installed' badge on the currently-installed version", async () => {
    const installed = plugin({
      source: "modrinth",
      source_project_id: "PROJ1",
      source_version_id: "V1",
    });
    mockBrowseAndDetail({
      plugins: [installed],
      projectId: "PROJ1",
      versions: [
        {
          version_id: "V1",
          version_number: "0.5.0",
          name: "Sodium 0.5.0",
          game_versions: ["1.21"],
        },
        {
          version_id: "V2",
          version_number: "0.6.0",
          name: "Sodium 0.6.0",
          game_versions: ["1.21"],
        },
      ],
    });
    renderTab();
    await waitFor(() => {
      expect(screen.getByText("Browse Modrinth")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Browse Modrinth"));

    // Click the search result to view versions.
    await waitFor(() => {
      expect(screen.getByText("Rendering engine")).toBeInTheDocument();
    });
    fireEvent.click(
      screen.getByText("Rendering engine").closest("button") as HTMLElement,
    );

    // The installed version shows "Installed" badge, not a button.
    await waitFor(() => {
      expect(screen.getByText("Installed")).toBeInTheDocument();
    });
  });

  it("shows 'Update' on other versions of an installed project", async () => {
    const installed = plugin({
      source: "modrinth",
      source_project_id: "PROJ1",
      source_version_id: "V1",
    });
    mockBrowseAndDetail({
      plugins: [installed],
      projectId: "PROJ1",
      versions: [
        {
          version_id: "V1",
          version_number: "0.5.0",
          name: "Sodium 0.5.0",
          game_versions: ["1.21"],
        },
        {
          version_id: "V2",
          version_number: "0.6.0",
          name: "Sodium 0.6.0",
          game_versions: ["1.21"],
        },
      ],
    });
    renderTab();
    await waitFor(() => {
      expect(screen.getByText("Browse Modrinth")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Browse Modrinth"));

    await waitFor(() => {
      expect(screen.getByText("Rendering engine")).toBeInTheDocument();
    });
    fireEvent.click(
      screen.getByText("Rendering engine").closest("button") as HTMLElement,
    );

    // The other version shows "Update" button.
    await waitFor(() => {
      // Find the Update button in the version list.
      const updateButtons = screen
        .getAllByText("Update")
        .filter((el) => el.tagName === "BUTTON");
      expect(updateButtons).toHaveLength(1);
    });
  });

  it("shows 'Install' on versions of a not-installed project", async () => {
    // No plugin installed for this project.
    mockBrowseAndDetail({
      plugins: [plugin()],
      projectId: "PROJ1",
      versions: [
        {
          version_id: "V1",
          version_number: "0.5.0",
          name: "Sodium 0.5.0",
          game_versions: ["1.21"],
        },
      ],
    });
    renderTab();
    await waitFor(() => {
      expect(screen.getByText("Browse Modrinth")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Browse Modrinth"));

    await waitFor(() => {
      expect(screen.getByText("Rendering engine")).toBeInTheDocument();
    });
    fireEvent.click(
      screen.getByText("Rendering engine").closest("button") as HTMLElement,
    );

    // Version shows "Install" button.
    await waitFor(() => {
      const installButtons = screen
        .getAllByText("Install")
        .filter((el) => el.tagName === "BUTTON");
      expect(installButtons).toHaveLength(1);
    });
    // No "Installed" badge or "Update" button.
    expect(screen.queryByText("Installed")).not.toBeInTheDocument();
  });
});

describe("Modrinth modal: simplified version display (issue #1354)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows version_number as primary and game_versions as secondary, without name", async () => {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes("/catalog/projects/")) {
        return Promise.resolve({
          project: {
            project_id: "PROJ1",
            slug: "sodium",
            title: "Sodium",
            description: "Rendering engine",
            author: "CaffeineMC",
            downloads: 50000,
            icon_url: null,
            categories: [],
            game_versions: ["1.21"],
            loaders: ["fabric"],
            body: "",
          },
          versions: [
            {
              version_id: "V1",
              version_number: "0.8.12-beta.1",
              name: "Sodium 0.8.12-beta.1 for Fabric 1.21.1",
              game_versions: ["1.21", "1.21.1"],
              date_published: "2026-06-20T00:00:00Z",
              dependencies: [],
              files: [],
              loaders: ["fabric"],
            },
          ],
        });
      }
      if (url.includes("/catalog/search")) {
        return Promise.resolve({
          hits: [
            {
              project_id: "PROJ1",
              slug: "sodium",
              title: "Sodium",
              description: "Rendering engine",
              author: "CaffeineMC",
              downloads: 50000,
              icon_url: null,
              categories: [],
              latest_game_versions: ["1.21"],
            },
          ],
          limit: 20,
          offset: 0,
          total_hits: 1,
        });
      }
      if (url.endsWith("/plugins/validate")) {
        return Promise.resolve(EMPTY_VALIDATION);
      }
      if (url.endsWith("/plugins/updates")) {
        return Promise.resolve({ updates: [] });
      }
      if (url.endsWith("/plugins")) {
        return Promise.resolve({ plugins: [plugin()] });
      }
      return Promise.resolve({});
    });

    renderTab();
    await waitFor(() => {
      expect(screen.getByText("Browse Modrinth")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Browse Modrinth"));

    // Click the search result to view versions.
    await waitFor(() => {
      expect(screen.getByText("Rendering engine")).toBeInTheDocument();
    });
    fireEvent.click(
      screen.getByText("Rendering engine").closest("button") as HTMLElement,
    );

    // The version number is displayed.
    await waitFor(() => {
      expect(screen.getByText("0.8.12-beta.1")).toBeInTheDocument();
    });
    // The game versions are displayed as secondary text.
    expect(screen.getByText("1.21, 1.21.1")).toBeInTheDocument();
    // The verbose name is NOT displayed.
    expect(
      screen.queryByText("Sodium 0.8.12-beta.1 for Fabric 1.21.1"),
    ).not.toBeInTheDocument();
  });
});

describe("ServerPluginsTab remove confirmation dialog (#1353)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a confirmation dialog with the plugin name on remove", async () => {
    mockGets({
      plugins: [plugin({ display_name: "Sodium" })],
      validation: EMPTY_VALIDATION,
    });
    renderTab();
    await waitFor(() => {
      expect(screen.getByText("Sodium")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Remove"));
    await waitFor(() => {
      expect(screen.getByText("Remove Sodium?")).toBeInTheDocument();
    });
    expect(
      screen.getByText(
        "This will permanently remove this mod from the server.",
      ),
    ).toBeInTheDocument();
  });

  it("does not call the delete API until confirmed", async () => {
    mockApi.delete.mockResolvedValue({});
    mockGets({
      plugins: [plugin({ display_name: "Sodium" })],
      validation: EMPTY_VALIDATION,
    });
    renderTab();
    await waitFor(() => {
      expect(screen.getByText("Sodium")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Remove"));
    await waitFor(() => {
      expect(screen.getByText("Remove Sodium?")).toBeInTheDocument();
    });
    // API should not have been called yet.
    expect(mockApi.delete).not.toHaveBeenCalled();
    // Click the destructive "Remove" confirm button in the dialog footer.
    // It is inside the modal-foot and does NOT have the .sm class (unlike the
    // table row action button which has .btn.sm.danger).
    const confirmBtn = screen
      .getAllByText("Remove")
      .find(
        (el) =>
          el.tagName === "BUTTON" &&
          el.classList.contains("danger") &&
          !el.classList.contains("sm"),
      );
    if (!confirmBtn) throw new Error("confirm button not found");
    fireEvent.click(confirmBtn);
    await waitFor(() => {
      expect(mockApi.delete).toHaveBeenCalledWith(
        expect.stringContaining("/plugins/p1"),
      );
    });
  });
});

describe("ServerPluginsTab dependencies toggle active state (#1357)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("adds active class to Dependencies button when expanded", async () => {
    mockGets({
      plugins: [plugin({ source: "modrinth" })],
      validation: EMPTY_VALIDATION,
    });
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes("/dependencies")) {
        return Promise.resolve({ dependencies: [] });
      }
      if (url.endsWith("/plugins/validate")) {
        return Promise.resolve(EMPTY_VALIDATION);
      }
      if (url.endsWith("/plugins/updates")) {
        return Promise.resolve({ updates: [] });
      }
      if (url.endsWith("/plugins")) {
        return Promise.resolve({
          plugins: [plugin({ source: "modrinth" })],
        });
      }
      return Promise.resolve({});
    });
    renderTab();
    await waitFor(() => {
      expect(screen.getByText("Sodium")).toBeInTheDocument();
    });
    const depsBtn = screen.getByText("Dependencies", { selector: "button" });
    // Initially not active.
    expect(depsBtn.classList.contains("active")).toBe(false);
    fireEvent.click(depsBtn);
    // After click, should have the active class.
    expect(depsBtn.classList.contains("active")).toBe(true);
  });
});

describe("ServerPluginsTab download button position (#1360)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the download button inside the toolbar row", async () => {
    mockDownload.downloadFile.mockResolvedValue(undefined);
    mockGets({
      plugins: [plugin({ side: "client" })],
      validation: EMPTY_VALIDATION,
    });
    renderTab();
    const button = await screen.findByText("Download client modpack");
    // The button should be inside the .plugins-toolbar wrapper (#1360).
    expect(button.closest(".plugins-toolbar")).not.toBeNull();
  });
});

describe("ServerPluginsTab upload progress (issue #1419)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a progress bar during JAR upload and hides it on success", async () => {
    mockGets({ plugins: [plugin()], validation: EMPTY_VALIDATION });

    let resolveUpload!: (value: unknown) => void;
    mockPostFormWithProgress.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    renderTab();
    await waitFor(() => {
      expect(screen.getByText("Sodium")).toBeInTheDocument();
    });

    // Trigger a file upload.
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    const file = new File(["x"], "test.jar", {
      type: "application/java-archive",
    });
    Object.defineProperty(file, "size", { value: 1024 });
    fireEvent.change(input, { target: { files: [file] } });

    // The progress bar should appear.
    await waitFor(() => {
      expect(screen.getByRole("progressbar")).toBeInTheDocument();
    });

    // Resolve the upload.
    resolveUpload({});

    // The progress bar should disappear.
    await waitFor(() => {
      expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    });
  });

  it("hides the progress bar on upload error", async () => {
    mockGets({ plugins: [plugin()], validation: EMPTY_VALIDATION });
    mockPostFormWithProgress.mockRejectedValue(
      new ApiError(500, { reason: "server_busy" }),
    );
    renderTab();
    await waitFor(() => {
      expect(screen.getByText("Sodium")).toBeInTheDocument();
    });

    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    const file = new File(["x"], "test.jar", {
      type: "application/java-archive",
    });
    Object.defineProperty(file, "size", { value: 1024 });
    fireEvent.change(input, { target: { files: [file] } });

    // The progress bar should disappear after the error.
    await waitFor(() => {
      expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    });
  });
});

describe("ServerPluginsTab Bedrock discovery hint (issue #1543)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const HINT_TEXT =
    "Bedrock players also need Floodgate. Install Floodgate-Spigot in one click from the plugin catalog, or download its jar from the";
  const HINT_LINK = "GeyserMC download page";

  // A real Geyser-Spigot install: mixed-case manifest identifier and a null
  // Modrinth project id. The gate must normalize case to detect it.
  const geyserByManifest = plugin({
    display_name: "Geyser-Spigot",
    mod_identifier: "Geyser-Spigot",
    source_project_id: null,
  });

  function renderTabFor(
    serverType: string,
    bedrockEnabled: boolean,
    plugins: unknown[] = [],
  ) {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    mockApi.get.mockImplementation((url: string) => {
      if (url.endsWith("/plugins/validate")) {
        return Promise.resolve(EMPTY_VALIDATION);
      }
      if (url.endsWith("/plugins/updates")) {
        return Promise.resolve({ updates: [] });
      }
      if (url.endsWith("/plugins")) {
        return Promise.resolve({ plugins });
      }
      if (url === "/api/meta") {
        return Promise.resolve({ bedrock_enabled: bedrockEnabled });
      }
      return Promise.resolve({});
    });
    return render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <ToastProvider>
            <ServerPluginsTab
              server={server({ server_type: serverType })}
              communityId={CID}
              can={allow}
            />
          </ToastProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
  }

  it("shows the hint when Geyser is detected by its mixed-case manifest", async () => {
    renderTabFor("paper", true, [geyserByManifest]);

    expect(await screen.findByText(HINT_TEXT)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: HINT_LINK });
    expect(link).toHaveAttribute(
      "href",
      "https://geysermc.org/download#floodgate",
    );
  });

  it.each([
    "wKkoqHrH",
    "geyser",
  ])("shows the hint when Geyser is a Modrinth catalog install (%s)", async (projectId) => {
    renderTabFor("paper", true, [
      plugin({
        display_name: "Geyser",
        mod_identifier: null,
        source_project_id: projectId,
      }),
    ]);

    expect(await screen.findByText(HINT_TEXT)).toBeInTheDocument();
  });

  it("hides the hint when paper + flag on but no Geyser plugin is installed", async () => {
    renderTabFor("paper", true, [plugin()]);

    await waitFor(() => {
      expect(screen.getByText("Sodium")).toBeInTheDocument();
    });
    expect(screen.queryByText(HINT_TEXT)).not.toBeInTheDocument();
  });

  it("hides the hint when the deployment flag is off, even with Geyser", async () => {
    renderTabFor("paper", false, [geyserByManifest]);

    await waitFor(() => {
      expect(screen.getByText("Geyser-Spigot")).toBeInTheDocument();
    });
    expect(screen.queryByText(HINT_TEXT)).not.toBeInTheDocument();
  });

  it("hides the hint for a non-paper server even when the flag is on", async () => {
    renderTabFor("fabric", true, [geyserByManifest]);

    await waitFor(() => {
      expect(screen.getByText("Geyser-Spigot")).toBeInTheDocument();
    });
    expect(screen.queryByText(HINT_TEXT)).not.toBeInTheDocument();
  });
});

describe("ServerPluginsTab refetch failure (#1805)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("keeps rendering cached plugins when a background refetch fails", async () => {
    mockGets({ plugins: [plugin()], validation: EMPTY_VALIDATION });
    const { queryClient } = renderTab();
    await screen.findByText("Sodium");

    // Simulate a transient API outage: the next background refetch fails.
    mockApi.get.mockRejectedValue(new ApiError(500, {}));
    await act(() => queryClient.invalidateQueries());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // The cached list stays on screen instead of the error.
    expect(screen.getByText("Sodium")).toBeInTheDocument();
    expect(screen.queryByText(/Could not load mods/)).not.toBeInTheDocument();
  });
});

describe("ServerPluginsTab Source column (issue #1934)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("labels a GeyserMC catalog install with its catalog, not 'Local'", async () => {
    mockGets({
      plugins: [
        plugin({
          display_name: "Floodgate-Spigot",
          source: "geyser",
          source_project_id: "floodgate",
        }),
      ],
      validation: EMPTY_VALIDATION,
    });
    renderTab();
    await screen.findByText("Floodgate-Spigot");

    expect(screen.getByText("GeyserMC")).toBeInTheDocument();
    expect(screen.queryByText("Local")).not.toBeInTheDocument();
  });

  it("labels a manual upload 'Local'", async () => {
    mockGets({
      plugins: [plugin({ source: "local" })],
      validation: EMPTY_VALIDATION,
    });
    renderTab();
    await screen.findByText("Sodium");

    expect(screen.getByText("Local")).toBeInTheDocument();
  });

  it("labels a provenance-unknown row 'Unknown', not 'Local' (issue #2059)", async () => {
    mockGets({
      plugins: [
        plugin({
          display_name: "Restored Mod",
          source: "unknown",
          source_project_id: null,
        }),
      ],
      validation: EMPTY_VALIDATION,
    });
    renderTab();
    await screen.findByText("Restored Mod");

    expect(screen.getByText("Unknown")).toBeInTheDocument();
    expect(screen.queryByText("Local")).not.toBeInTheDocument();
  });
});
