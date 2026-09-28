import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { setAccessToken } from "../auth/tokenStore.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { meta } from "../test/meta.ts";
import { installMockWebSocket } from "../test/mockWebSocket.ts";
import { ServerDetailPage } from "./ServerDetailPage.tsx";

const CID = "c1";
const SID = "s1";

const mockApi = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
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

// Only `downloadFile` is stubbed; the rest of the module stays real so the
// exports the detail page reaches for (`saveUrlAs`) survive the mock (#2359).
const mockDownload = vi.hoisted(() => ({ downloadFile: vi.fn() }));
vi.mock("../api/download.ts", async () => {
  const actual =
    await vi.importActual<typeof import("../api/download.ts")>(
      "../api/download.ts",
    );
  return { ...actual, ...mockDownload };
});

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
  resource_pack_prompt: null as string | null,
};

// Route api.get by path: server detail, resource-pack assignment, resource-packs
// library list, meta (consumed by the Settings tab).
function routeGet(
  opts: {
    srv?: Record<string, unknown>;
    assignment?: typeof ASSIGNMENT | null;
    packs?: (typeof PACK)[];
    assignmentError?: unknown;
  } = {},
) {
  const srv = server(opts.srv);
  const assignment = opts.assignment === undefined ? null : opts.assignment;
  const packs = opts.packs ?? [PACK];
  mockApi.get.mockImplementation((path: string) => {
    if (path.endsWith("/resource-pack")) {
      if (opts.assignmentError !== undefined) {
        return Promise.reject(opts.assignmentError);
      }
      // "No pack assigned" is a 200 with a null body, not a 404 (issue #2238).
      return Promise.resolve(assignment);
    }
    if (path === "/api/resource-packs") {
      return Promise.resolve({ resource_packs: packs });
    }
    if (path === "/api/meta") {
      return Promise.resolve(meta());
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

function dialogSubmit() {
  const dialog = screen.getByRole("dialog");
  return dialog.querySelector(".modal-foot .btn.primary") as HTMLButtonElement;
}

function removeConfirm() {
  const dialog = screen.getByRole("dialog");
  return dialog.querySelector(".modal-foot .btn.danger") as HTMLButtonElement;
}

let restoreWs: () => void;
beforeEach(() => {
  setAccessToken("tok-1");
  mockApi.get.mockReset();
  mockApi.post.mockReset();
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

describe("ServerResourcePackSection — assignment load error", () => {
  it("shows the load error and suppresses assignment controls", async () => {
    // Simulate a 500 error (non-404) on the assignment endpoint.
    routeGet({ assignmentError: new ApiError(500, {}) });
    await openSettings();

    expect(
      await screen.findByText(t("serverDetail.resourcePack.loadError")),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: t("serverDetail.resourcePack.assign"),
      }),
    ).not.toBeInTheDocument();
  });
});

describe("ServerResourcePackSection — action gating", () => {
  it("shows the 'no pack assigned' message when none is assigned", async () => {
    routeGet({ assignment: null });
    await openSettings();

    expect(
      await screen.findByText(t("serverDetail.resourcePack.none")),
    ).toBeInTheDocument();
  });

  it.each([
    [
      "hides Assign without server:update",
      null,
      false,
      false,
      "assign",
      "hidden",
    ],
    [
      "disables Assign while the server is running",
      null,
      true,
      true,
      "assign",
      "disabled",
    ],
    [
      "hides Change and Remove without server:update",
      ASSIGNMENT,
      false,
      false,
      "assigned",
      "hidden",
    ],
    [
      "disables Change and Remove while the server is running",
      ASSIGNMENT,
      true,
      true,
      "assigned",
      "disabled",
    ],
  ] as const)(
    "%s",
    async (_caseId, assignment, canUpdate, serverRunning, actionSet, outcome) => {
      mockCan = canUpdate ? () => true : (code) => code !== "server:update";
      routeGet({
        srv: serverRunning
          ? { observed_state: "running", desired_state: "running" }
          : undefined,
        assignment,
      });
      await openSettings();

      if (assignment === null) {
        await screen.findByText(t("serverDetail.resourcePack.none"));
      } else {
        await screen.findByText(PACK.display_name);
      }

      const buttonNames =
        actionSet === "assign"
          ? [t("serverDetail.resourcePack.assign")]
          : [
              t("serverDetail.resourcePack.change"),
              t("serverDetail.resourcePack.remove"),
            ];
      for (const name of buttonNames) {
        const button = screen.queryByRole("button", { name });
        if (outcome === "hidden") {
          expect(button).not.toBeInTheDocument();
        } else {
          expect(button).toBeInTheDocument();
          expect(button).toBeDisabled();
        }
      }

      if (serverRunning) {
        expect(
          screen.getByText(t("serverDetail.resourcePack.notAtRest")),
        ).toBeInTheDocument();
      }
    },
  );
});

describe("ServerResourcePackSection — assigned state", () => {
  it.each([
    [
      "pack details",
      ASSIGNMENT,
      [
        [PACK.display_name, 1],
        [PACK.filename, 1],
        ["1.0 MiB", 1],
        [PACK.sha1_hash, 1],
        [t("serverDetail.resourcePack.notRequired"), 1],
        [t("serverDetail.resourcePack.promptNone"), 1],
      ],
    ],
    [
      "required option",
      { ...ASSIGNMENT, require_resource_pack: true },
      [[t("serverDetail.resourcePack.required"), 2]],
    ],
    [
      "custom prompt option",
      { ...ASSIGNMENT, resource_pack_prompt: "Please accept the pack!" },
      [["Please accept the pack!", 1]],
    ],
    [
      "null prompt option",
      { ...ASSIGNMENT, resource_pack_prompt: null },
      [[t("serverDetail.resourcePack.promptNone"), 1]],
    ],
  ] as const)("renders %s", async (_caseId, assignment, expectedRows) => {
    routeGet({ assignment });
    await openSettings();

    await screen.findByText(PACK.display_name);
    for (const [text, minimumOccurrences] of expectedRows) {
      expect(screen.getAllByText(text).length).toBeGreaterThanOrEqual(
        minimumOccurrences,
      );
    }
  });
});

describe("ServerResourcePackSection — assign flow", () => {
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
});

describe("ServerResourcePackSection — unassign flow", () => {
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
});

describe("ServerResourcePackSection — unsettled mutation errors", () => {
  it.each([
    ["assign", null],
    ["unassign", ASSIGNMENT],
  ] as const)(
    "shows the unsettled error for %s",
    async (action, assignment) => {
      routeGet({ assignment });
      if (action === "assign") {
        mockApi.post.mockRejectedValue(
          new ApiError(409, { reason: "server_unsettled" }),
        );
      } else {
        mockApi.delete.mockRejectedValue(
          new ApiError(409, { reason: "server_unsettled" }),
        );
      }
      await openSettings();

      if (action === "assign") {
        fireEvent.click(
          await screen.findByRole("button", {
            name: t("serverDetail.resourcePack.assign"),
          }),
        );
        const select = await screen.findByRole("combobox");
        fireEvent.change(select, { target: { value: PACK.id } });
        fireEvent.click(dialogSubmit());
      } else {
        fireEvent.click(
          await screen.findByRole("button", {
            name: t("serverDetail.resourcePack.remove"),
          }),
        );
        fireEvent.click(removeConfirm());
      }

      expect(
        await screen.findByText(t("serverDetail.error.unsettled")),
      ).toBeInTheDocument();
    },
  );
});

describe("ServerResourcePackSection — version < 1.17 option wiring", () => {
  it("hides option controls in the assign dialog", async () => {
    routeGet({ srv: { mc_version: "1.16.4" }, assignment: null });
    await openSettings();

    fireEvent.click(
      await screen.findByRole("button", {
        name: t("serverDetail.resourcePack.assign"),
      }),
    );

    // The pack select should still be present
    const select = await screen.findByRole("combobox");
    expect(select).toBeInTheDocument();

    // Checkbox and text input should NOT be present
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    const dialog = screen.getByRole("dialog");
    expect(dialog.querySelector('input[type="text"]')).not.toBeInTheDocument();
  });

  it("forces safe defaults when editing an existing assignment on < 1.17", async () => {
    routeGet({
      srv: { mc_version: "1.16.4" },
      assignment: {
        ...ASSIGNMENT,
        require_resource_pack: true,
        resource_pack_prompt: "Hello",
      },
    });
    mockApi.post.mockResolvedValue(ASSIGNMENT);
    await openSettings();

    // Open the assign (change) dialog
    fireEvent.click(
      await screen.findByRole("button", {
        name: t("serverDetail.resourcePack.change"),
      }),
    );

    const select = await screen.findByRole("combobox");
    fireEvent.change(select, { target: { value: PACK.id } });
    fireEvent.click(dialogSubmit());

    // Even though the existing assignment had require=true and prompt="Hello",
    // the mutation must send safe defaults because the server is < 1.17.
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
});
