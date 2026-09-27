// @vitest-environment jsdom
// Pinned to jsdom: the WAI-ARIA tabpanel focus test relies on jsdom's focus
// handling (happy-dom may not replicate tabIndex-based focusability faithfully).

/**
 * Page-specific tab tests for the community-settings page (#514, #2058):
 * deep links land on the named tab, and a permission-denied panel remains
 * keyboard reachable. Generic URL and WAI-ARIA tab behavior lives in
 * urlState.test.tsx.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
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

function renderPage(path = `/communities/${CID}/settings`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
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

describe("CommunitySettingsPage page-specific tabs (#514, #2058)", () => {
  beforeEach(() => {
    setAccessToken("tok-1");
    mockApi.get.mockReset();
    setCommunityId.mockReset();
    mockCan = () => true;
    routeGet();
  });
  afterEach(() => vi.clearAllMocks());

  it("deep-links to the tab named by the URL hash", async () => {
    renderPage(`/communities/${CID}/settings#audit`);
    await screen.findAllByText("Sakura");
    expect(activeTab()).toBe(t("communitySettings.tab.audit"));
  });

  it("a tabpanel whose content has no focusable element is focusable (#2058)", async () => {
    // A user without member:read lands on a permission-denied message — a bare
    // <p> with no focusable descendant. Without tabIndex={0} on the panel,
    // activating the tab strands the keyboard user.
    // APG tabs: the panel is focusable when it has no focusable element or its
    // first element with content is not focusable.
    mockCan = () => false;
    renderPage();
    await screen.findAllByText("Sakura");

    const panel = screen.getByRole("tabpanel");
    // Precondition: nothing inside the panel can take focus.
    expect(
      panel.querySelectorAll(
        "a[href], button, input, select, textarea, [tabindex]",
      ),
    ).toHaveLength(0);

    panel.focus();
    expect(panel).toHaveFocus();
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
