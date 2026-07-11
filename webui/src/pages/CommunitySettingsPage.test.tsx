/**
 * URL-driven tab tests for the community-settings page (#514): the active tab
 * lives in the URL hash (WEBUI_SPEC.md Section 5 names #members / #audit ...),
 * deep links land on the named tab, switching pushes history, and Back restores
 * the prior tab (simulated with MemoryRouter + a navigate(-1) probe).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { setAccessToken } from "../auth/tokenStore.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { CommunitySettingsPage } from "./CommunitySettingsPage.tsx";

const CID = "c1";

const mockApi = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

vi.mock("../api/client.ts", async () => {
  const actual =
    await vi.importActual<typeof import("../api/client.ts")>(
      "../api/client.ts",
    );
  return { ...actual, api: mockApi };
});

let mockCan: Can = () => true;
const setCommunityId = vi.fn();
vi.mock("../permissions/ActiveCommunityProvider.tsx", () => ({
  useActiveCommunity: () => ({
    communityId: CID,
    setCommunityId,
    communities: [{ id: CID, name: "Sakura" }],
  }),
}));
vi.mock("../permissions/useCan.ts", () => ({ useCan: () => mockCan }));

// The page reads the community; each tab reads its own collection. Anything that
// is not a known collection resolves to the bare community object.
function routeGet() {
  mockApi.get.mockImplementation((path: string) => {
    if (path.startsWith(`/api/communities/${CID}/members`)) {
      return Promise.resolve([]);
    }
    if (path.startsWith(`/api/communities/${CID}/roles`)) {
      return Promise.resolve([]);
    }
    if (path.startsWith(`/api/communities/${CID}/grants`)) {
      return Promise.resolve([]);
    }
    if (path.startsWith(`/api/communities/${CID}/groups`)) {
      return Promise.resolve([]);
    }
    if (path.startsWith(`/api/communities/${CID}/audit`)) {
      return Promise.resolve({ records: [] });
    }
    return Promise.resolve({ id: CID, name: "Sakura" });
  });
}

// A history probe: drives navigate(-1) so a test can simulate the Back button.
function BackProbe() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(-1)}>
      router-back
    </button>
  );
}

let lastHash = "";
function HashProbe() {
  lastHash = useLocation().hash;
  return null;
}

function renderPage(path = `/communities/${CID}/settings`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <BackProbe />
          <HashProbe />
          <Routes>
            <Route
              path="/communities/:cid/settings"
              element={<CommunitySettingsPage />}
            />
          </Routes>
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { ...result, queryClient };
}

function activeTab(): string | null {
  return (
    screen
      .getAllByRole("tab")
      .find((el) => el.getAttribute("aria-selected") === "true")?.textContent ??
    null
  );
}

describe("CommunitySettingsPage URL-driven tabs (#514)", () => {
  beforeEach(() => {
    setAccessToken("tok-1");
    mockApi.get.mockReset();
    setCommunityId.mockReset();
    mockCan = () => true;
    routeGet();
  });
  afterEach(() => vi.clearAllMocks());

  it("defaults to Members with a clean (hash-less) URL", async () => {
    renderPage();
    await screen.findAllByText("Sakura");
    expect(activeTab()).toBe(t("communitySettings.tab.members"));
    expect(lastHash).toBe("");
  });

  it("deep-links to the tab named by the URL hash", async () => {
    renderPage(`/communities/${CID}/settings#audit`);
    await screen.findAllByText("Sakura");
    expect(activeTab()).toBe(t("communitySettings.tab.audit"));
  });

  it("switching a tab writes its hash", async () => {
    renderPage();
    await screen.findAllByText("Sakura");

    fireEvent.click(
      screen.getByRole("tab", { name: t("communitySettings.tab.roles") }),
    );
    expect(activeTab()).toBe(t("communitySettings.tab.roles"));
    expect(lastHash).toBe("#roles");
  });

  it("Back restores the previously active tab", async () => {
    renderPage();
    await screen.findAllByText("Sakura");

    fireEvent.click(
      screen.getByRole("tab", { name: t("communitySettings.tab.roles") }),
    );
    fireEvent.click(
      screen.getByRole("tab", { name: t("communitySettings.tab.audit") }),
    );
    expect(activeTab()).toBe(t("communitySettings.tab.audit"));

    fireEvent.click(screen.getByText("router-back"));
    await waitFor(() =>
      expect(activeTab()).toBe(t("communitySettings.tab.roles")),
    );

    fireEvent.click(screen.getByText("router-back"));
    await waitFor(() =>
      expect(activeTab()).toBe(t("communitySettings.tab.members")),
    );
  });

  it("tab buttons carry aria-controls and the panel carries aria-labelledby (#1216)", async () => {
    renderPage();
    await screen.findAllByText("Sakura");

    const membersTab = screen.getByRole("tab", {
      name: t("communitySettings.tab.members"),
    });
    expect(membersTab).toHaveAttribute("aria-controls", "cs-panel-members");
    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveAttribute("id", "cs-panel-members");
    expect(panel).toHaveAttribute("aria-labelledby", "cs-tab-members");
  });

  it("ArrowRight moves focus to the next tab (#1216)", async () => {
    renderPage();
    await screen.findAllByText("Sakura");

    const membersTab = screen.getByRole("tab", {
      name: t("communitySettings.tab.members"),
    });
    membersTab.focus();
    fireEvent.keyDown(membersTab, { key: "ArrowRight" });

    const rolesTab = screen.getByRole("tab", {
      name: t("communitySettings.tab.roles"),
    });
    expect(rolesTab).toHaveFocus();
    expect(rolesTab).toHaveAttribute("aria-selected", "true");
  });

  it("inactive tabs have tabIndex -1 (roving tabindex, #1216)", async () => {
    renderPage();
    await screen.findAllByText("Sakura");

    const membersTab = screen.getByRole("tab", {
      name: t("communitySettings.tab.members"),
    });
    const rolesTab = screen.getByRole("tab", {
      name: t("communitySettings.tab.roles"),
    });
    expect(membersTab).toHaveAttribute("tabindex", "0");
    expect(rolesTab).toHaveAttribute("tabindex", "-1");
  });
});

describe("CommunitySettingsPage refetch failure (#1797)", () => {
  beforeEach(() => {
    setAccessToken("tok-1");
    mockApi.get.mockReset();
    setCommunityId.mockReset();
    mockCan = () => true;
    routeGet();
  });
  afterEach(() => vi.clearAllMocks());

  it("keeps rendering the cached community when a background refetch fails", async () => {
    const { queryClient } = renderPage();
    await screen.findAllByText("Sakura");

    // Simulate a transient API outage: the next background refetch fails.
    mockApi.get.mockRejectedValue(new ApiError(500, {}));
    await act(() => queryClient.invalidateQueries());
    // The query-state notification lands a task after invalidateQueries
    // settles; flush it so the assertion sees the post-refetch render.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // The cached page stays on screen instead of a full-page error.
    expect(screen.getAllByText("Sakura").length).toBeGreaterThan(0);
    expect(
      screen.queryByText(t("communitySettings.loadError")),
    ).not.toBeInTheDocument();
  });
});
