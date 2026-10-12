// @vitest-environment jsdom
// Keep jsdom for tabpanel focus behavior.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
    // A denied panel has no focusable child; the panel itself must remain keyboard reachable.
    mockCan = () => false;
    renderPage();
    await screen.findAllByText("Sakura");

    const panel = screen.getByRole("tabpanel");
    expect(
      panel.querySelectorAll(
        "a[href], button, input, select, textarea, [tabindex]",
      ),
    ).toHaveLength(0);

    panel.focus();
    expect(panel).toHaveFocus();
  });
});
