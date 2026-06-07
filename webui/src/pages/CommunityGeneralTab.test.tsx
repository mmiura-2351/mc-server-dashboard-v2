import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
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

// Spy on the rendered location so we can assert the post-delete navigation.
let lastPath = "";
function LocationProbe() {
  lastPath = useLocation().pathname;
  return null;
}

function routeGet() {
  mockApi.get.mockImplementation((path: string) => {
    if (path === `/api/communities/${CID}/members`) {
      return Promise.resolve([]);
    }
    return Promise.resolve({ id: CID, name: "Sakura" });
  });
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={[`/communities/${CID}/settings`]}>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <LocationProbe />
          <Routes>
            <Route
              path="/communities/:cid/settings"
              element={<CommunitySettingsPage />}
            />
            <Route path="/" element={<div>landing</div>} />
          </Routes>
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

async function openGeneral() {
  await screen.findAllByText("Sakura");
  fireEvent.click(
    screen.getByRole("tab", { name: t("communitySettings.tab.general") }),
  );
}

describe("CommunityGeneralTab", () => {
  beforeEach(() => {
    setAccessToken("tok-1");
    mockApi.get.mockReset();
    mockApi.post.mockReset();
    mockApi.patch.mockReset();
    mockApi.put.mockReset();
    mockApi.delete.mockReset();
    mockCan = () => true;
    setCommunityId.mockReset();
    lastPath = "";
  });
  afterEach(() => vi.clearAllMocks());

  it("renames the community with a PATCH carrying the new name", async () => {
    routeGet();
    mockApi.patch.mockResolvedValue({ id: CID, name: "Sakura SMP" });
    renderPage();
    await openGeneral();

    const input = await screen.findByLabelText(
      t("communitySettings.general.nameLabel"),
    );
    fireEvent.change(input, { target: { value: "Sakura SMP" } });
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.general.save"),
      }),
    );

    await waitFor(() => {
      expect(mockApi.patch).toHaveBeenCalledWith(`/api/communities/${CID}`, {
        body: JSON.stringify({ name: "Sakura SMP" }),
      });
    });
  });

  it("surfaces a name_taken rename rejection as a toast", async () => {
    routeGet();
    mockApi.patch.mockRejectedValue(
      new ApiError(409, { reason: "name_taken" }),
    );
    renderPage();
    await openGeneral();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.general.save"),
      }),
    );

    expect(
      await screen.findByText(t("communitySettings.general.nameTaken")),
    ).toBeInTheDocument();
  });

  it("deletes after the typed confirm, then resets the active community and navigates to the landing", async () => {
    routeGet();
    mockApi.delete.mockResolvedValue(undefined);
    renderPage();
    await openGeneral();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.general.deleteButton"),
      }),
    );
    // Typed-confirm uses the community name as the phrase.
    fireEvent.change(screen.getByPlaceholderText("Sakura"), {
      target: { value: "Sakura" },
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.general.deleteConfirm"),
      }),
    );

    await waitFor(() => {
      expect(mockApi.delete).toHaveBeenCalledWith(`/api/communities/${CID}`);
    });
    // The deleted community must not stay active, and the user lands on "/".
    await waitFor(() => expect(setCommunityId).toHaveBeenCalledWith(null));
    await waitFor(() => expect(lastPath).toBe("/"));
  });

  it("hides the danger zone without community:delete", async () => {
    mockCan = (code: string) => code !== "community:delete";
    routeGet();
    renderPage();
    await openGeneral();

    expect(
      await screen.findByLabelText(t("communitySettings.general.nameLabel")),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: t("communitySettings.general.deleteButton"),
      }),
    ).not.toBeInTheDocument();
  });

  it("disables the rename field without community:update", async () => {
    mockCan = (code: string) => code !== "community:update";
    routeGet();
    renderPage();
    await openGeneral();

    const input = await screen.findByLabelText(
      t("communitySettings.general.nameLabel"),
    );
    expect(input).toBeDisabled();
  });
});
