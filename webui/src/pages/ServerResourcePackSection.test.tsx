import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
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
    server_type: "paper",
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

const PACK = {
  id: "pack-1",
  display_name: "My Texture Pack",
  filename: "my-pack.zip",
  description: null,
  download_url: "https://cdn.example.com/packs/pack-1/my-pack.zip",
  size_bytes: 1048576,
  sha1_hash: "aabbccdd11223344",
  sha256_hash: "deadbeef",
  created_at: "2026-06-10T00:00:00Z",
  updated_at: "2026-06-10T00:00:00Z",
  uploaded_by: "user-1",
};

const ASSIGNMENT = {
  assigned_at: "2026-06-15T00:00:00Z",
  assigned_by: "user-1",
  require_resource_pack: false,
  resource_pack: PACK,
  resource_pack_prompt: null,
};

// Route api.get by path: server detail, resource-pack assignment, resource-packs
// library list, meta (consumed by the Settings tab).
function routeGet(
  opts: {
    srv?: Record<string, unknown>;
    assignment?: typeof ASSIGNMENT | null;
    packs?: (typeof PACK)[];
  } = {},
) {
  const srv = server(opts.srv);
  const assignment = opts.assignment === undefined ? null : opts.assignment;
  const packs = opts.packs ?? [PACK];
  mockApi.get.mockImplementation((path: string) => {
    if (path.endsWith("/resource-pack")) {
      if (assignment === null) {
        return Promise.reject(new ApiError(404, { reason: "not_found" }));
      }
      return Promise.resolve(assignment);
    }
    if (path === "/api/resource-packs") {
      return Promise.resolve({ resource_packs: packs });
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

describe("ServerResourcePackSection — unassigned state", () => {
  it("shows the 'no pack assigned' message when none is assigned", async () => {
    routeGet({ assignment: null });
    await openSettings();

    expect(
      await screen.findByText(t("serverDetail.resourcePack.none")),
    ).toBeInTheDocument();
  });

  it("shows the Assign button when the user has server:update", async () => {
    routeGet({ assignment: null });
    await openSettings();

    expect(
      await screen.findByRole("button", {
        name: t("serverDetail.resourcePack.assign"),
      }),
    ).toBeInTheDocument();
  });

  it("hides the Assign button when the user lacks server:update", async () => {
    mockCan = (code) => code !== "server:update";
    routeGet({ assignment: null });
    await openSettings();

    await screen.findByText(t("serverDetail.resourcePack.none"));
    expect(
      screen.queryByRole("button", {
        name: t("serverDetail.resourcePack.assign"),
      }),
    ).not.toBeInTheDocument();
  });

  it("disables the Assign button when the server is running", async () => {
    routeGet({
      srv: { observed_state: "running", desired_state: "running" },
      assignment: null,
    });
    await openSettings();

    const btn = await screen.findByRole("button", {
      name: t("serverDetail.resourcePack.assign"),
    });
    expect(btn).toBeDisabled();
    expect(
      screen.getByText(t("serverDetail.resourcePack.notAtRest")),
    ).toBeInTheDocument();
  });
});

describe("ServerResourcePackSection — assigned state", () => {
  it("shows pack details when a pack is assigned", async () => {
    routeGet({ assignment: ASSIGNMENT });
    await openSettings();

    expect(await screen.findByText("My Texture Pack")).toBeInTheDocument();
    expect(screen.getByText("my-pack.zip")).toBeInTheDocument();
    expect(screen.getByText("1.0 MiB")).toBeInTheDocument();
    expect(screen.getByText("aabbccdd11223344")).toBeInTheDocument();
  });

  it("shows require_resource_pack status", async () => {
    routeGet({
      assignment: { ...ASSIGNMENT, require_resource_pack: true },
    });
    await openSettings();

    // The "Required" label appears as the dt AND the dd value.
    const requiredElements = await screen.findAllByText(
      t("serverDetail.resourcePack.required"),
    );
    expect(requiredElements.length).toBeGreaterThanOrEqual(2);
  });

  it("shows resource_pack_prompt when set", async () => {
    routeGet({
      assignment: {
        ...ASSIGNMENT,
        resource_pack_prompt: "Please accept the pack!",
      },
    });
    await openSettings();

    expect(
      await screen.findByText("Please accept the pack!"),
    ).toBeInTheDocument();
  });

  it("shows 'None' for prompt when null", async () => {
    routeGet({
      assignment: { ...ASSIGNMENT, resource_pack_prompt: null },
    });
    await openSettings();

    expect(
      await screen.findByText(t("serverDetail.resourcePack.promptNone")),
    ).toBeInTheDocument();
  });

  it("shows Change and Remove buttons", async () => {
    routeGet({ assignment: ASSIGNMENT });
    await openSettings();

    expect(
      await screen.findByRole("button", {
        name: t("serverDetail.resourcePack.change"),
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: t("serverDetail.resourcePack.remove"),
      }),
    ).toBeInTheDocument();
  });

  it("disables Change and Remove when server is running", async () => {
    routeGet({
      srv: { observed_state: "running", desired_state: "running" },
      assignment: ASSIGNMENT,
    });
    await openSettings();

    const change = await screen.findByRole("button", {
      name: t("serverDetail.resourcePack.change"),
    });
    const remove = screen.getByRole("button", {
      name: t("serverDetail.resourcePack.remove"),
    });
    expect(change).toBeDisabled();
    expect(remove).toBeDisabled();
  });

  it("hides Change and Remove without server:update", async () => {
    mockCan = (code) => code !== "server:update";
    routeGet({ assignment: ASSIGNMENT });
    await openSettings();

    await screen.findByText("My Texture Pack");
    expect(
      screen.queryByRole("button", {
        name: t("serverDetail.resourcePack.change"),
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: t("serverDetail.resourcePack.remove"),
      }),
    ).not.toBeInTheDocument();
  });
});

describe("ServerResourcePackSection — assign flow", () => {
  // The section's "Assign" button and the dialog's submit "Assign" button
  // share the same translated text; helper finds the dialog's submit inside
  // the modal.
  function dialogSubmit() {
    const dialog = screen.getByRole("dialog");
    return dialog.querySelector(
      ".modal-foot .btn.primary",
    ) as HTMLButtonElement;
  }

  it("opens the assign dialog and submits", async () => {
    routeGet({ assignment: null });
    mockApi.post.mockResolvedValue(ASSIGNMENT);
    await openSettings();

    // Only the section button exists before the dialog opens.
    fireEvent.click(
      await screen.findByRole("button", {
        name: t("serverDetail.resourcePack.assign"),
      }),
    );

    // The dialog shows pack list in a select
    const select = await screen.findByRole("combobox");
    expect(select).toBeInTheDocument();

    // Submit is disabled until a pack is selected
    const submit = dialogSubmit();
    expect(submit).toBeDisabled();

    // Select a pack
    fireEvent.change(select, { target: { value: PACK.id } });
    expect(submit).not.toBeDisabled();

    // Submit
    fireEvent.click(submit);

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/resource-pack`,
        {
          body: JSON.stringify({
            resource_pack_id: PACK.id,
            require_resource_pack: false,
            resource_pack_prompt: null,
          }),
        },
      ),
    );
  });

  it("sends require_resource_pack and resource_pack_prompt when set", async () => {
    routeGet({ assignment: null });
    mockApi.post.mockResolvedValue(ASSIGNMENT);
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("serverDetail.resourcePack.assign"),
      }),
    );

    const select = await screen.findByRole("combobox");
    fireEvent.change(select, { target: { value: PACK.id } });

    // Check require
    fireEvent.click(screen.getByRole("checkbox"));

    // Set prompt — find the text input inside the dialog, not the settings form
    const dialog = screen.getByRole("dialog");
    const promptInput = dialog.querySelector(
      'input[type="text"]',
    ) as HTMLInputElement;
    fireEvent.change(promptInput, {
      target: { value: "Accept our texture pack!" },
    });

    fireEvent.click(dialogSubmit());

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/resource-pack`,
        {
          body: JSON.stringify({
            resource_pack_id: PACK.id,
            require_resource_pack: true,
            resource_pack_prompt: "Accept our texture pack!",
          }),
        },
      ),
    );
  });

  it("shows the empty message when no packs are available", async () => {
    routeGet({ assignment: null, packs: [] });
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("serverDetail.resourcePack.assign"),
      }),
    );

    expect(
      await screen.findByText(
        t("serverDetail.resourcePack.assignDialog.empty"),
      ),
    ).toBeInTheDocument();
  });

  it("surfaces a 409 server_unsettled error on assign", async () => {
    routeGet({ assignment: null });
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "server_unsettled" }),
    );
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("serverDetail.resourcePack.assign"),
      }),
    );
    const select = await screen.findByRole("combobox");
    fireEvent.change(select, { target: { value: PACK.id } });
    fireEvent.click(dialogSubmit());

    expect(
      await screen.findByText(t("serverDetail.error.unsettled")),
    ).toBeInTheDocument();
  });
});

describe("ServerResourcePackSection — unassign flow", () => {
  // The remove confirmation button lives inside the modal dialog.
  function removeConfirm() {
    const dialog = screen.getByRole("dialog");
    return dialog.querySelector(".modal-foot .btn.danger") as HTMLButtonElement;
  }

  it("opens the remove dialog and submits", async () => {
    routeGet({ assignment: ASSIGNMENT });
    mockApi.delete.mockResolvedValue(undefined);
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("serverDetail.resourcePack.remove"),
      }),
    );

    // Confirmation dialog appears
    expect(
      screen.getByText(t("serverDetail.resourcePack.removeDialog.body")),
    ).toBeInTheDocument();

    // Confirm
    fireEvent.click(removeConfirm());

    await waitFor(() =>
      expect(mockApi.delete).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/${SID}/resource-pack`,
      ),
    );
  });

  it("cancels the remove dialog", async () => {
    routeGet({ assignment: ASSIGNMENT });
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("serverDetail.resourcePack.remove"),
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: t("common.cancel") }));

    // Dialog closed, no API call
    expect(
      screen.queryByText(t("serverDetail.resourcePack.removeDialog.body")),
    ).not.toBeInTheDocument();
    expect(mockApi.delete).not.toHaveBeenCalled();
  });

  it("surfaces a 409 server_unsettled error on unassign", async () => {
    routeGet({ assignment: ASSIGNMENT });
    mockApi.delete.mockRejectedValue(
      new ApiError(409, { reason: "server_unsettled" }),
    );
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("serverDetail.resourcePack.remove"),
      }),
    );
    fireEvent.click(removeConfirm());

    expect(
      await screen.findByText(t("serverDetail.error.unsettled")),
    ).toBeInTheDocument();
  });
});
