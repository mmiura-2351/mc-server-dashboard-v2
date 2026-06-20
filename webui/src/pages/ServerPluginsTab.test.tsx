/**
 * Tests for the dependency/compatibility validation checklist (issue #1307).
 *
 * Renders the plugins tab with a mocked API returning installed plugins and a
 * validation payload, and asserts the checklist surfaces each finding kind (or
 * the all-clear message when none).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
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
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ToastProvider>
          <ServerPluginsTab server={server()} communityId={CID} can={allow} />
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
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

  it("does not render the checklist when no plugins are installed", async () => {
    mockGets({ plugins: [], validation: EMPTY_VALIDATION });
    renderTab();
    await waitFor(() => {
      expect(screen.getByText("No plugins installed.")).toBeInTheDocument();
    });
    expect(
      screen.queryByText("Dependencies & compatibility"),
    ).not.toBeInTheDocument();
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
    const select = await screen.findByLabelText<HTMLSelectElement>("Side");
    expect(select.value).toBe("client");
  });

  it("posts the side override on change", async () => {
    mockApi.post.mockResolvedValue({});
    mockGets({ plugins: [plugin()], validation: EMPTY_VALIDATION });
    renderTab();
    const select = await screen.findByLabelText<HTMLSelectElement>("Side");
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
        "client-modpack.zip",
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
});
