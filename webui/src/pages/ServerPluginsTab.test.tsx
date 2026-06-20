/**
 * Tests for the dependency/compatibility validation checklist (issue #1307).
 *
 * Renders the plugins tab with a mocked API returning installed plugins and a
 * validation payload, and asserts the checklist surfaces each finding kind (or
 * the all-clear message when none).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
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
