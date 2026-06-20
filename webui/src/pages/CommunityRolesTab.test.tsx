import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { setAccessToken } from "../auth/tokenStore.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import { COMMUNITY_PERMISSION_FAMILIES } from "../permissions/catalog.ts";
import type { Can } from "../permissions/useCan.ts";
import { CommunitySettingsPage } from "./CommunitySettingsPage.tsx";

const CID = "c1";

const ALL_CODES = COMMUNITY_PERMISSION_FAMILIES.flatMap(({ codes }) => codes);

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

function community() {
  return { id: CID, name: "Sakura" };
}

function role(over: Record<string, unknown> = {}) {
  return {
    id: "r1",
    name: "Moderator",
    is_preset: false,
    permissions: [],
    ...over,
  };
}

function routeGet(roles: unknown[]) {
  mockApi.get.mockImplementation((path: string) => {
    if (path === `/api/communities/${CID}/roles`) {
      return Promise.resolve(roles);
    }
    return Promise.resolve(community());
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
}

// The Roles tab is not the default; switch to it after the page loads.
async function openRolesTab() {
  await screen.findAllByText("Sakura");
  fireEvent.click(
    screen.getByRole("tab", { name: t("communitySettings.tab.roles") }),
  );
}

async function openEditor(roles: unknown[]) {
  routeGet(roles);
  renderPage();
  await openRolesTab();
  fireEvent.click(
    await screen.findByRole("button", {
      name: t("communitySettings.roles.create"),
    }),
  );
}

describe("CommunityRolesTab", () => {
  beforeEach(() => {
    setAccessToken("tok-1");
    mockApi.get.mockReset();
    mockApi.post.mockReset();
    mockApi.patch.mockReset();
    mockApi.put.mockReset();
    mockApi.delete.mockReset();
    mockCan = () => true;
    setCommunityId.mockReset();
  });
  afterEach(() => vi.clearAllMocks());

  it("lists roles and locks the preset Owner (no edit/delete)", async () => {
    routeGet([
      role({ id: "ro", name: "Owner", is_preset: true }),
      role({ id: "r1", name: "Moderator" }),
    ]);
    renderPage();
    await openRolesTab();

    const ownerRow = (await screen.findByText("Owner")).closest("tr");
    const modRow = screen.getByText("Moderator").closest("tr");
    if (ownerRow === null || modRow === null) {
      throw new Error("rows missing");
    }
    // Preset Owner shows the preset badge and offers no edit/delete affordances.
    expect(
      ownerRow.querySelector("button"),
      "preset role must have no action buttons",
    ).toBeNull();
    expect(modRow.querySelector("button")).not.toBeNull();
  });

  it("renders the full 33-code matrix grouped by family, derived from the catalog", async () => {
    await openEditor([]);

    // Every family has a select-all checkbox labelled from its family label,
    // and every one of the catalog codes renders as a checkbox.
    // Count tracks the catalog size; update when new families are added.
    const total = ALL_CODES.length;
    expect(total).toBe(33); // 30 original + session:read (#961) + plugin:read/manage (#1153)
    const checkboxes = await screen.findAllByRole("checkbox");
    // One per code + one select-all per family.
    expect(checkboxes).toHaveLength(
      total + COMMUNITY_PERMISSION_FAMILIES.length,
    );

    for (const { family } of COMMUNITY_PERMISSION_FAMILIES) {
      expect(
        screen.getByLabelText(
          `${t("communitySettings.roles.selectAll")}: ${t(`communitySettings.roles.family.${family}`)}`,
        ),
      ).toBeInTheDocument();
    }
  });

  it("select-all toggles every code in its family", async () => {
    await openEditor([]);

    const serverFamily = COMMUNITY_PERMISSION_FAMILIES[0];
    const selectAll = await screen.findByLabelText(
      `${t("communitySettings.roles.selectAll")}: ${t(`communitySettings.roles.family.${serverFamily.family}`)}`,
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.roles.nameLabel")),
      { target: { value: "Builder" } },
    );
    fireEvent.click(selectAll);
    fireEvent.click(
      screen.getByRole("button", { name: t("communitySettings.roles.save") }),
    );

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalled();
    });
    const body = JSON.parse(mockApi.post.mock.calls[0][1].body);
    expect(new Set(body.permissions)).toEqual(new Set(serverFamily.codes));
  });

  it("creates a role with a POST carrying {name, permissions}", async () => {
    await openEditor([]);
    mockApi.post.mockResolvedValue(role());

    fireEvent.change(
      screen.getByLabelText(t("communitySettings.roles.nameLabel")),
      { target: { value: "Builder" } },
    );
    // Tick one specific code via its checkbox (labelled by its full code).
    fireEvent.click(screen.getByLabelText("server:read"));
    fireEvent.click(
      screen.getByRole("button", { name: t("communitySettings.roles.save") }),
    );

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/roles`,
        {
          body: JSON.stringify({
            name: "Builder",
            permissions: ["server:read"],
          }),
        },
      );
    });
  });

  it("edits a role with a PATCH, prefilling existing permissions", async () => {
    routeGet([
      role({ id: "r1", name: "Moderator", permissions: ["server:read"] }),
    ]);
    mockApi.patch.mockResolvedValue(role());
    renderPage();
    await openRolesTab();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.roles.edit"),
      }),
    );
    // Existing permission is pre-checked.
    expect(
      (screen.getByLabelText("server:read") as HTMLInputElement).checked,
    ).toBe(true);
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.roles.nameLabel")),
      { target: { value: "Mod2" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("communitySettings.roles.save") }),
    );

    await waitFor(() => {
      expect(mockApi.patch).toHaveBeenCalledWith(
        `/api/communities/${CID}/roles/r1`,
        {
          body: JSON.stringify({ name: "Mod2", permissions: ["server:read"] }),
        },
      );
    });
  });

  it("deletes a role after the typed confirm, with a DELETE", async () => {
    routeGet([role({ id: "r1", name: "Moderator" })]);
    mockApi.delete.mockResolvedValue(undefined);
    renderPage();
    await openRolesTab();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.roles.delete"),
      }),
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.roles.deleteConfirm"),
      }),
    );

    await waitFor(() => {
      expect(mockApi.delete).toHaveBeenCalledWith(
        `/api/communities/${CID}/roles/r1`,
      );
    });
  });

  it("surfaces a delete failure with a toast", async () => {
    routeGet([role({ id: "r1", name: "Moderator" })]);
    mockApi.delete.mockRejectedValue(new ApiError(409, { reason: "conflict" }));
    renderPage();
    await openRolesTab();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.roles.delete"),
      }),
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.roles.deleteConfirm"),
      }),
    );

    expect(
      await screen.findByText(t("communitySettings.roles.deleteError")),
    ).toBeInTheDocument();
  });

  it("surfaces a 409 name_taken on create as an inline message", async () => {
    await openEditor([]);
    mockApi.post.mockRejectedValue(new ApiError(409, { reason: "name_taken" }));

    fireEvent.change(
      screen.getByLabelText(t("communitySettings.roles.nameLabel")),
      { target: { value: "Moderator" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("communitySettings.roles.save") }),
    );

    expect(
      await screen.findByText(t("communitySettings.roles.errNameTaken")),
    ).toBeInTheDocument();
  });

  it("hides create/edit/delete controls without role:manage", async () => {
    mockCan = (code: string) => code !== "role:manage";
    routeGet([role({ id: "r1", name: "Moderator" })]);
    renderPage();
    await openRolesTab();

    await screen.findByText("Moderator");
    expect(
      screen.queryByRole("button", {
        name: t("communitySettings.roles.create"),
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: t("communitySettings.roles.edit"),
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: t("communitySettings.roles.delete"),
      }),
    ).not.toBeInTheDocument();
  });

  it("shows the denied notice when role:read is absent", async () => {
    mockCan = (code: string) => code !== "role:read";
    routeGet([]);
    renderPage();
    await openRolesTab();

    expect(
      await screen.findByText(t("permissions.denied")),
    ).toBeInTheDocument();
  });

  it("routes a 403 on create through onForbidden (named-permission toast)", async () => {
    await openEditor([]);
    mockApi.post.mockRejectedValue(
      new ApiError(403, { reason: "forbidden", permission: "role:manage" }),
    );

    fireEvent.change(
      screen.getByLabelText(t("communitySettings.roles.nameLabel")),
      { target: { value: "Builder" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("communitySettings.roles.save") }),
    );

    expect(
      await screen.findByText(`${t("permissions.deniedNamed")}role:manage`),
    ).toBeInTheDocument();
  });
});
