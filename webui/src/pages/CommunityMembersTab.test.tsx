import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

function community() {
  return { id: CID, name: "Sakura" };
}

function member(over: Record<string, unknown> = {}) {
  return {
    membership_id: "m1",
    user_id: "u1",
    username: "alice",
    role_names: ["Owner"],
    ...over,
  };
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

// Route `api.get` by path: the page reads the community, the Members tab reads
// the member list and (when the picker is shown) the role list.
function routeGet(opts: { members?: unknown[]; roles?: unknown[] }) {
  mockApi.get.mockImplementation((path: string) => {
    if (path === `/communities/${CID}/members`) {
      return Promise.resolve(opts.members ?? []);
    }
    if (path === `/communities/${CID}/roles`) {
      return Promise.resolve(opts.roles ?? []);
    }
    // Bare community fetch.
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

// The Members tab is the default tab, so the page lands on it directly.
async function awaitMembers() {
  await screen.findAllByText("Sakura");
}

describe("CommunityMembersTab", () => {
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

  it("lists members with their usernames and role chips", async () => {
    routeGet({
      members: [
        member({
          membership_id: "m1",
          username: "alice",
          role_names: ["Owner"],
        }),
        member({
          membership_id: "m2",
          user_id: "u2",
          username: "bob",
          role_names: ["Moderator"],
        }),
      ],
      roles: [role()],
    });
    renderPage();
    await awaitMembers();

    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.getByText("bob")).toBeInTheDocument();
    expect(screen.getByText("Owner")).toBeInTheDocument();
    expect(screen.getByText("Moderator")).toBeInTheDocument();
  });

  it("shows the empty state when there are no members", async () => {
    routeGet({ members: [] });
    renderPage();
    await awaitMembers();

    expect(
      await screen.findByText(t("communitySettings.members.empty")),
    ).toBeInTheDocument();
  });

  it("adds a member by username with a POST carrying {username}", async () => {
    routeGet({ members: [], roles: [] });
    mockApi.post.mockResolvedValue({ membership_id: "m9", user_id: "u9" });
    renderPage();
    await awaitMembers();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.members.add"),
      }),
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.members.usernameLabel")),
      { target: { value: "carol" } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.members.addSubmit"),
      }),
    );

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(`/communities/${CID}/members`, {
        body: JSON.stringify({ username: "carol" }),
      });
    });
  });

  it("surfaces a 422 user_not_found as an inline message", async () => {
    routeGet({ members: [], roles: [] });
    mockApi.post.mockRejectedValue(
      new ApiError(422, { reason: "user_not_found" }),
    );
    renderPage();
    await awaitMembers();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.members.add"),
      }),
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.members.usernameLabel")),
      { target: { value: "ghost" } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.members.addSubmit"),
      }),
    );

    expect(
      await screen.findByText(t("communitySettings.members.errUserNotFound")),
    ).toBeInTheDocument();
  });

  it("surfaces a 409 already_member as an inline message", async () => {
    routeGet({ members: [], roles: [] });
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "already_member" }),
    );
    renderPage();
    await awaitMembers();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.members.add"),
      }),
    );
    fireEvent.change(
      screen.getByLabelText(t("communitySettings.members.usernameLabel")),
      { target: { value: "alice" } },
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.members.addSubmit"),
      }),
    );

    expect(
      await screen.findByText(t("communitySettings.members.errAlreadyMember")),
    ).toBeInTheDocument();
  });

  it("assigns a role with a POST carrying {role_id}", async () => {
    routeGet({
      members: [member({ role_names: [] })],
      roles: [role({ id: "r1", name: "Moderator" })],
    });
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await awaitMembers();

    await screen.findByText("alice");
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.members.assignRole"),
      }),
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "Moderator" }));

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith(
        `/communities/${CID}/members/u1/roles`,
        { body: JSON.stringify({ role_id: "r1" }) },
      );
    });
  });

  it("unassigns a role with a DELETE to the role route", async () => {
    routeGet({
      members: [member({ role_names: ["Moderator"] })],
      roles: [role({ id: "r1", name: "Moderator" })],
    });
    mockApi.delete.mockResolvedValue(undefined);
    renderPage();
    await awaitMembers();

    await screen.findByText("Moderator");
    fireEvent.click(
      screen.getByRole("button", {
        name: `${t("communitySettings.members.unassignRole")}: Moderator`,
      }),
    );

    await waitFor(() => {
      expect(mockApi.delete).toHaveBeenCalledWith(
        `/communities/${CID}/members/u1/roles/r1`,
      );
    });
  });

  it("removes a member after the typed confirm, with a DELETE", async () => {
    routeGet({ members: [member({ username: "alice" })], roles: [] });
    mockApi.delete.mockResolvedValue(undefined);
    renderPage();
    await awaitMembers();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.members.remove"),
      }),
    );
    // The confirm explains role/grant revocation.
    expect(
      screen.getByText(t("communitySettings.members.removeDialogBody")),
    ).toBeInTheDocument();
    // Typed-confirm: typing the username enables the destructive button.
    fireEvent.change(screen.getByPlaceholderText("alice"), {
      target: { value: "alice" },
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.members.removeConfirm"),
      }),
    );

    await waitFor(() => {
      expect(mockApi.delete).toHaveBeenCalledWith(
        `/communities/${CID}/members/u1`,
      );
    });
  });

  it("surfaces a removal failure with the remove-specific copy", async () => {
    routeGet({ members: [member({ username: "alice" })], roles: [] });
    mockApi.delete.mockRejectedValue(new ApiError(500, {}));
    renderPage();
    await awaitMembers();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("communitySettings.members.remove"),
      }),
    );
    fireEvent.change(screen.getByPlaceholderText("alice"), {
      target: { value: "alice" },
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.members.removeConfirm"),
      }),
    );

    expect(
      await screen.findByText(t("communitySettings.members.removeError")),
    ).toBeInTheDocument();
  });

  it("hides add/remove/role controls without the matching permissions", async () => {
    // No member:add, member:remove, or role:manage.
    mockCan = (code: string) =>
      code !== "member:add" &&
      code !== "member:remove" &&
      code !== "role:manage";
    routeGet({
      members: [member({ role_names: ["Moderator"] })],
      roles: [role()],
    });
    renderPage();
    await awaitMembers();

    await screen.findByText("alice");
    expect(
      screen.queryByRole("button", {
        name: t("communitySettings.members.add"),
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: t("communitySettings.members.remove"),
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: t("communitySettings.members.assignRole"),
      }),
    ).not.toBeInTheDocument();
  });

  it("shows the denied notice when member:read is absent", async () => {
    mockCan = (code: string) => code !== "member:read";
    routeGet({ members: [] });
    renderPage();
    await awaitMembers();

    expect(
      await screen.findByText(t("permissions.denied")),
    ).toBeInTheDocument();
  });

  it("routes a 403 on assign through onForbidden (named-permission toast)", async () => {
    routeGet({
      members: [member({ role_names: [] })],
      roles: [role({ id: "r1", name: "Moderator" })],
    });
    mockApi.post.mockRejectedValue(
      new ApiError(403, { reason: "role:manage" }),
    );
    renderPage();
    await awaitMembers();

    await screen.findByText("alice");
    fireEvent.click(
      screen.getByRole("button", {
        name: t("communitySettings.members.assignRole"),
      }),
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "Moderator" }));

    expect(
      await screen.findByText(`${t("permissions.deniedNamed")}role:manage`),
    ).toBeInTheDocument();
  });
});
