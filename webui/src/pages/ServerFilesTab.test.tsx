import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { setAccessToken } from "../auth/tokenStore.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { installMockWebSocket } from "../test/mockWebSocket.ts";
import { encodeUtf8Base64 } from "./fileText.ts";
import { ServerDetailPage } from "./ServerDetailPage.tsx";
import { versionDate } from "./ServerFilesTab.tsx";

const CID = "c1";
const SID = "s1";

const mockApi = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));

const mockPostFormWithProgress = vi.hoisted(() => vi.fn());

vi.mock("../api/client.ts", async () => {
  const actual =
    await vi.importActual<typeof import("../api/client.ts")>(
      "../api/client.ts",
    );
  return {
    ...actual,
    api: mockApi,
    postFormWithProgress: mockPostFormWithProgress,
  };
});

const mockDownload = vi.hoisted(() => ({
  downloadFile: vi.fn(),
  fetchFileBlob: vi.fn(),
}));
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

// Use the real router (incl. useNavigate): switching to the Files tab now
// drives the URL hash (#514), so navigate must update the location, not no-op.

const FILES_BASE = `/api/communities/${CID}/servers/${SID}/files`;

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
    ...overrides,
  };
}

function listing(
  entries: { name: string; is_dir: boolean; size?: number }[],
  truncated = false,
) {
  return {
    path: "",
    truncated,
    entries: entries.map((e) => ({ size: 0, ...e })),
  };
}

// Route the mocked `api.get` by URL: server detail vs file list vs file content.
function routeGet(handlers: {
  detail?: unknown;
  list?: unknown;
  content?: unknown;
}) {
  mockApi.get.mockImplementation((path: string) => {
    if (path.includes("/files?path=") && !path.includes("list=")) {
      return Promise.resolve(handlers.content);
    }
    if (path.includes("/files?path=")) {
      return Promise.resolve(handlers.list);
    }
    return Promise.resolve(handlers.detail);
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

async function openFiles() {
  await screen.findByText("survival");
  fireEvent.click(
    screen.getByRole("tab", { name: t("serverDetail.tab.files") }),
  );
}

// Install the mock socket in every describe that renders the detail page: the
// events client opens a WS, and a missing mock has caused CI flakes (it would
// fire onDown -> invalidate and refetch out from under the test).
// jsdom lacks URL.createObjectURL/revokeObjectURL; stub them so the ZIP
// download path in bulkDownload() doesn't throw.
if (typeof URL.createObjectURL !== "function") {
  URL.createObjectURL = vi.fn(() => "blob:fake");
}
if (typeof URL.revokeObjectURL !== "function") {
  URL.revokeObjectURL = vi.fn();
}

let restoreWs: () => void;
beforeEach(() => {
  restoreWs = installMockWebSocket();
  setAccessToken("tok-1");
  mockApi.get.mockReset();
  mockApi.post.mockReset();
  mockApi.put.mockReset();
  mockApi.patch.mockReset();
  mockApi.delete.mockReset();
  mockPostFormWithProgress.mockReset();
  mockDownload.downloadFile.mockReset();
  mockDownload.downloadFile.mockResolvedValue(undefined);
  mockDownload.fetchFileBlob.mockReset();
  mockDownload.fetchFileBlob.mockResolvedValue(new Blob(["test"]));
  mockCan = () => true;
});
afterEach(() => {
  restoreWs();
  vi.clearAllMocks();
});

describe("ServerFilesTab listing", () => {
  it("lists the working-set root directory", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "world", is_dir: true },
        { name: "server.properties", is_dir: false },
      ]),
    });
    renderPage();
    await openFiles();

    expect(await screen.findByText(/world/)).toBeInTheDocument();
    expect(screen.getByText(/server\.properties/)).toBeInTheDocument();
    await waitFor(() =>
      expect(mockApi.get).toHaveBeenCalledWith(`${FILES_BASE}?path=&list=true`),
    );
  });

  it("exposes the full file name via title on the truncating name cell", async () => {
    const longName = "a-very-long-file-name-that-the-ellipsis-truncates.txt";
    routeGet({
      detail: server(),
      list: listing([{ name: longName, is_dir: false }]),
    });
    renderPage();
    await openFiles();

    const cell = await screen.findByText(new RegExp(longName));
    expect(cell.closest("button.file-name")).toHaveAttribute("title", longName);
  });

  it("shows the truncated notice when the listing was clipped", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a", is_dir: false }], true),
    });
    renderPage();
    await openFiles();

    expect(await screen.findByText(t("files.truncated"))).toBeInTheDocument();
  });

  it("navigates into a directory and re-lists with the child path", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("path=world")) {
        return Promise.resolve(listing([{ name: "level.dat", is_dir: false }]));
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(listing([{ name: "world", is_dir: true }]));
      }
      return Promise.resolve(server());
    });
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/world/));
    await waitFor(() =>
      expect(mockApi.get).toHaveBeenCalledWith(
        `${FILES_BASE}?path=world&list=true`,
      ),
    );
  });
});

describe("ServerFilesTab viewer / editor", () => {
  it("opens a text file in an editor and round-trips a unicode save (base64 PUT)", async () => {
    const original = "motd=ようこそ 🐉\n";
    routeGet({
      detail: server(),
      list: listing([{ name: "server.properties", is_dir: false }]),
      content: {
        path: "server.properties",
        content_base64: encodeUtf8Base64(original),
      },
    });
    mockApi.put.mockResolvedValue(undefined);
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/server\.properties/));
    const editor = (await screen.findByLabelText(
      t("files.editorLabel"),
    )) as HTMLTextAreaElement;
    expect(editor.value).toBe(original);

    const edited = "motd=こんにちは 🎮\n";
    fireEvent.change(editor, { target: { value: edited } });
    fireEvent.click(screen.getByRole("button", { name: t("files.save") }));

    await waitFor(() => expect(mockApi.put).toHaveBeenCalled());
    const [putUrl, putInit] = mockApi.put.mock.calls[0];
    expect(putUrl).toBe(`${FILES_BASE}?path=server.properties`);
    const body = JSON.parse((putInit as { body: string }).body);
    // The PUT carries the edited text as UTF-8-safe base64.
    expect(body.content_base64).toBe(encodeUtf8Base64(edited));
  });

  it("offers download only for a binary file (no editor) and shows metadata", async () => {
    const binary = btoa(String.fromCharCode(0x50, 0x4b, 0x03, 0x04, 0x00));
    routeGet({
      detail: server(),
      list: listing([{ name: "region.mca", is_dir: false, size: 2048 }]),
      content: { path: "region.mca", content_base64: binary },
    });
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/region\.mca/));
    expect(
      await screen.findByText(t("files.cannotPreview")),
    ).toBeInTheDocument();
    expect(screen.getByText(/2\.0 KB/)).toBeInTheDocument();
    expect(
      screen.queryByLabelText(t("files.editorLabel")),
    ).not.toBeInTheDocument();
  });

  it("hides the viewer pane when no file is selected", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // The viewer pane should not be rendered.
    expect(document.querySelector(".file-viewer")).toBeNull();
    // The layout should be single-pane (no two-pane class).
    expect(document.querySelector(".file-layout.two-pane")).toBeNull();
  });

  it("shows the viewer when a file is selected and closes on close button", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
      content: {
        path: "readme.txt",
        content_base64: encodeUtf8Base64("hello"),
      },
    });
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/readme\.txt/));
    // Wait for content to load and viewer pane to appear with two-pane layout.
    await screen.findByLabelText(t("files.editorLabel"));
    expect(document.querySelector(".file-layout.two-pane")).not.toBeNull();
    expect(document.querySelector(".file-viewer")).not.toBeNull();

    // Close the viewer.
    fireEvent.click(
      screen.getByRole("button", { name: t("files.closeViewer") }),
    );
    await waitFor(() =>
      expect(document.querySelector(".file-viewer")).toBeNull(),
    );
    expect(document.querySelector(".file-layout.two-pane")).toBeNull();
  });
});

describe("ServerFilesTab operations", () => {
  it("uploads via context menu with extract=false", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Right-click on a file row to open context menu.
    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    // Click Upload in context menu.
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.upload") }),
    );

    // The hidden file input should be present; simulate choosing a file.
    const file = new File(["x"], "world.zip");
    fireEvent.change(screen.getByLabelText(t("files.contextMenu.upload")), {
      target: { files: [file] },
    });

    await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
    const [url, form] = mockPostFormWithProgress.mock.calls[0];
    expect(url).toBe(`${FILES_BASE}/upload?path=&extract=false`);
    expect((form as FormData).get("file")).toBe(file);
  });

  it("creates a directory via context menu", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Right-click to open context menu.
    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.newFolder") }),
    );
    fireEvent.change(screen.getByLabelText(t("files.folderName")), {
      target: { value: "datapacks" },
    });
    fireEvent.click(screen.getByRole("button", { name: t("files.create") }));

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `${FILES_BASE}/directories?path=datapacks`,
      ),
    );
  });

  it("renames an entry via context menu with a {from, to} body", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "old.txt", is_dir: false }]),
    });
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/old\.txt/);

    // Right-click to open context menu.
    const row = screen.getByText(/old\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.rename") }),
    );
    const input = screen.getByLabelText(t("files.newName"));
    fireEvent.change(input, { target: { value: "new.txt" } });
    const confirm = screen
      .getAllByRole("button", { name: t("files.rename") })
      .at(-1) as HTMLButtonElement;
    fireEvent.click(confirm);

    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    const [url, init] = mockApi.post.mock.calls[0];
    expect(url).toBe(`${FILES_BASE}/rename`);
    expect(JSON.parse((init as { body: string }).body)).toEqual({
      from: "old.txt",
      to: "new.txt",
    });
  });

  it("deletes via context menu after confirm with ?path=", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "junk.txt", is_dir: false }]),
    });
    mockApi.delete.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/junk\.txt/);

    // Right-click to open context menu.
    const row = screen.getByText(/junk\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.delete") }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("files.delete.confirm") }),
    );

    await waitFor(() =>
      expect(mockApi.delete).toHaveBeenCalledWith(
        `${FILES_BASE}?path=junk.txt`,
      ),
    );
  });

  it("downloads a file via context menu", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "log.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/log\.txt/);

    // Right-click to open context menu.
    const row = screen.getByText(/log\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.download") }),
    );
    await waitFor(() =>
      expect(mockDownload.downloadFile).toHaveBeenCalledWith(
        `${FILES_BASE}/download?path=log.txt`,
        "log.txt",
      ),
    );
  });
});

describe("ServerFilesTab permission gating", () => {
  it("denies the tab entirely without file:read", async () => {
    mockCan = (code) => code !== "file:read";
    routeGet({ detail: server(), list: listing([]) });
    renderPage();
    await openFiles();

    expect(await screen.findByText(t("files.denied"))).toBeInTheDocument();
  });

  it("hides write controls without file:edit", async () => {
    mockCan = (code) => code !== "file:edit";
    routeGet({
      detail: server(),
      list: listing([{ name: "server.properties", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/server\.properties/);

    // Right-click to open context menu — rename/delete/upload/newFolder should be hidden.
    const row = screen
      .getByText(/server\.properties/)
      .closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    expect(
      screen.queryByRole("menuitem", { name: t("files.contextMenu.rename") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", { name: t("files.contextMenu.delete") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", { name: t("files.contextMenu.upload") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", {
        name: t("files.contextMenu.newFolder"),
      }),
    ).not.toBeInTheDocument();
  });

  it("routes a 403 through onForbidden (named-permission toast)", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "x", is_dir: false }]),
    });
    mockApi.delete.mockRejectedValue(
      new ApiError(403, { reason: "forbidden", permission: "file:edit" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText("x");

    // Use context menu to delete.
    const row = screen.getByText("x").closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.delete") }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("files.delete.confirm") }),
    );

    expect(
      await screen.findByText(
        t("permissions.deniedNamed", { permission: "file:edit" }),
      ),
    ).toBeInTheDocument();
  });
});

describe("ServerFilesTab search", () => {
  it("posts a {query, by, max_results} body and opens a hit in the viewer", async () => {
    mockApi.get.mockImplementation((path: string) => {
      // After clicking a hit, the browser re-lists the hit's parent directory.
      if (path.includes("path=world") && path.includes("list=")) {
        return Promise.resolve(listing([{ name: "level.dat", is_dir: false }]));
      }
      if (path.includes("/files/history")) {
        return Promise.resolve({ path: "world/level.dat", versions: [] });
      }
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return Promise.resolve({
          path: "world/level.dat",
          content_base64: encodeUtf8Base64("seed=42\n"),
        });
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(listing([]));
      }
      return Promise.resolve(server());
    });
    mockApi.post.mockResolvedValue({
      paths: ["world/level.dat"],
      truncated: false,
    });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    fireEvent.change(screen.getByLabelText(t("files.search.label")), {
      target: { value: "level" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("files.search.submit") }),
    );

    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    const [searchUrl, searchInit] = mockApi.post.mock.calls[0];
    expect(searchUrl).toBe(`${FILES_BASE}/search`);
    expect(JSON.parse((searchInit as { body: string }).body)).toEqual({
      query: "level",
      by: "name",
      max_results: 100,
    });

    // The hit is clickable and opens it in the viewer.
    fireEvent.click(await screen.findByText("/world/level.dat"));
    await waitFor(() =>
      expect(mockApi.get).toHaveBeenCalledWith(
        `${FILES_BASE}?path=world%2Flevel.dat`,
      ),
    );
  });

  it("searches by content and encodes a path with a space/ampersand hit", async () => {
    routeGet({ detail: server(), list: listing([]) });
    mockApi.post.mockResolvedValue({
      paths: ["config/a b & c.yml"],
      truncated: false,
    });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    fireEvent.click(screen.getByLabelText(t("files.search.byContent")));
    fireEvent.change(screen.getByLabelText(t("files.search.label")), {
      target: { value: "token" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("files.search.submit") }),
    );

    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    expect(JSON.parse(mockApi.post.mock.calls[0][1].body).by).toBe("content");

    // The encoded hit drives a content GET whose ?path= is fully URL-encoded.
    fireEvent.click(await screen.findByText("/config/a b & c.yml"));
    await waitFor(() =>
      expect(mockApi.get).toHaveBeenCalledWith(
        `${FILES_BASE}?path=${encodeURIComponent("config/a b & c.yml")}`,
      ),
    );
  });
});

// Realistic version IDs: {ns_timestamp:020d}-{random_hex8}.
const VID1 = "01750852800000000000-a1b2c3d4"; // 2025-06-25T12:00:00Z
const VID2 = "01750939200000000000-b2c3d4e5"; // 2025-06-26T12:00:00Z

describe("versionDate helper", () => {
  it("converts a nanosecond-timestamp version ID to the correct Date", () => {
    const d = versionDate(VID1);
    expect(d.toISOString()).toBe("2025-06-25T12:00:00.000Z");
  });
});

describe("ServerFilesTab history + rollback", () => {
  it("shows formatted dates instead of raw version IDs", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("/files/history")) {
        return Promise.resolve({
          path: "a b.txt",
          versions: [VID1, VID2],
        });
      }
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return Promise.resolve({
          path: "a b.txt",
          content_base64: encodeUtf8Base64("hi\n"),
        });
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(listing([{ name: "a b.txt", is_dir: false }]));
      }
      return Promise.resolve(server());
    });
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/a b\.txt/));
    await screen.findByLabelText(t("files.editorLabel"));
    fireEvent.click(screen.getByRole("button", { name: t("files.history") }));

    // Dates are rendered via toLocaleString, not the raw version IDs.
    const date1 = versionDate(VID1).toLocaleString();
    const date2 = versionDate(VID2).toLocaleString();
    expect(await screen.findByText(date1)).toBeInTheDocument();
    expect(screen.getByText(date2)).toBeInTheDocument();

    // Raw version IDs should NOT appear as visible text.
    expect(screen.queryByText(VID1)).not.toBeInTheDocument();
    expect(screen.queryByText(VID2)).not.toBeInTheDocument();

    expect(screen.getByText(t("files.history.hint"))).toBeInTheDocument();
    await waitFor(() =>
      expect(mockApi.get).toHaveBeenCalledWith(
        `${FILES_BASE}/history?path=a%20b.txt`,
      ),
    );
  });

  it("rolls back to a version after confirm with {version_id} body and an encoded path", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("/files/history")) {
        return Promise.resolve({ path: "a b.txt", versions: [VID1] });
      }
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return Promise.resolve({
          path: "a b.txt",
          content_base64: encodeUtf8Base64("hi\n"),
        });
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(listing([{ name: "a b.txt", is_dir: false }]));
      }
      return Promise.resolve(server());
    });
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/a b\.txt/));
    await screen.findByLabelText(t("files.editorLabel"));
    fireEvent.click(screen.getByRole("button", { name: t("files.history") }));
    const date1 = versionDate(VID1).toLocaleString();
    await screen.findByText(date1);

    fireEvent.click(
      screen.getByRole("button", { name: t("files.history.rollback") }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("files.rollback.confirm") }),
    );

    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    const [url, init] = mockApi.post.mock.calls[0];
    expect(url).toBe(`${FILES_BASE}/rollback?path=a%20b.txt`);
    // The raw version ID is sent to the API, not the formatted date.
    expect(JSON.parse((init as { body: string }).body)).toEqual({
      version_id: VID1,
    });
  });

  it("Escape closes only the rollback confirm, leaving the history drawer open", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("/files/history")) {
        return Promise.resolve({ path: "a b.txt", versions: [VID1] });
      }
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return Promise.resolve({
          path: "a b.txt",
          content_base64: encodeUtf8Base64("hi\n"),
        });
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(listing([{ name: "a b.txt", is_dir: false }]));
      }
      return Promise.resolve(server());
    });
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/a b\.txt/));
    await screen.findByLabelText(t("files.editorLabel"));
    fireEvent.click(screen.getByRole("button", { name: t("files.history") }));
    await screen.findByText(versionDate(VID1).toLocaleString());

    // Open the stacked rollback confirm on top of the history drawer.
    fireEvent.click(
      screen.getByRole("button", { name: t("files.history.rollback") }),
    );
    expect(
      screen.getByRole("button", { name: t("files.rollback.confirm") }),
    ).toBeInTheDocument();

    // One Escape closes only the topmost (confirm); the history drawer stays.
    fireEvent.keyDown(document, { key: "Escape" });
    expect(
      screen.queryByRole("button", { name: t("files.rollback.confirm") }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(t("files.history.hint"))).toBeInTheDocument();
  });

  it("hides the History button without file:history", async () => {
    mockCan = (code) => code !== "file:history";
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return Promise.resolve({
          path: "a.txt",
          content_base64: encodeUtf8Base64("hi\n"),
        });
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(listing([{ name: "a.txt", is_dir: false }]));
      }
      return Promise.resolve(server());
    });
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/a\.txt/));
    await screen.findByLabelText(t("files.editorLabel"));
    expect(
      screen.queryByRole("button", { name: t("files.history") }),
    ).not.toBeInTheDocument();
  });

  it("omits the rollback button without file:rollback", async () => {
    mockCan = (code) => code !== "file:rollback";
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("/files/history")) {
        return Promise.resolve({ path: "a.txt", versions: [VID1] });
      }
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return Promise.resolve({
          path: "a.txt",
          content_base64: encodeUtf8Base64("hi\n"),
        });
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(listing([{ name: "a.txt", is_dir: false }]));
      }
      return Promise.resolve(server());
    });
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/a\.txt/));
    await screen.findByLabelText(t("files.editorLabel"));
    fireEvent.click(screen.getByRole("button", { name: t("files.history") }));
    await screen.findByText(versionDate(VID1).toLocaleString());

    expect(
      screen.queryByRole("button", { name: t("files.history.rollback") }),
    ).not.toBeInTheDocument();
  });
});

describe("ServerFilesTab running notice", () => {
  it("shows the live-working-set notice when the server is running", async () => {
    routeGet({
      detail: server({ observed_state: "running", desired_state: "running" }),
      list: listing([]),
    });
    renderPage();
    await openFiles();

    act(() => undefined);
    expect(
      await screen.findByText(t("files.runningNotice")),
    ).toBeInTheDocument();
  });

  it("omits the notice when the server is stopped", async () => {
    routeGet({
      detail: server({ observed_state: "stopped" }),
      list: listing([]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    expect(
      screen.queryByText(t("files.runningNotice")),
    ).not.toBeInTheDocument();
  });

  it("hides Upload and New folder in context menu while the server is running", async () => {
    routeGet({
      detail: server({ observed_state: "running", desired_state: "running" }),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.runningNotice"));

    // Right-click to open context menu.
    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    expect(
      screen.queryByRole("menuitem", { name: t("files.contextMenu.upload") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", {
        name: t("files.contextMenu.newFolder"),
      }),
    ).not.toBeInTheDocument();
  });

  it("shows Upload and New folder in context menu while the server is stopped", async () => {
    routeGet({
      detail: server({ observed_state: "stopped" }),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Right-click to open context menu.
    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    expect(
      screen.getByRole("menuitem", { name: t("files.contextMenu.upload") }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: t("files.contextMenu.newFolder") }),
    ).toBeInTheDocument();
  });

  it("hides Upload and New folder in context menu while the server is stopping (transitional)", async () => {
    routeGet({
      detail: server({ observed_state: "stopping", desired_state: "stopped" }),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.runningNotice"));

    // Right-click to open context menu.
    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    expect(
      screen.queryByRole("menuitem", { name: t("files.contextMenu.upload") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", {
        name: t("files.contextMenu.newFolder"),
      }),
    ).not.toBeInTheDocument();
  });
});

describe("ServerFilesTab 409 reason toasts", () => {
  it("maps server_unsettled to the stop-the-server message on upload", async () => {
    // Use a stopped server; the API then returns a 409 to exercise the error
    // handler (e.g. a race: server started between the UI check and the API call).
    routeGet({
      detail: server({ observed_state: "stopped" }),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    mockPostFormWithProgress.mockRejectedValue(
      new ApiError(409, { reason: "server_unsettled" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Right-click to open context menu and trigger upload.
    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.upload") }),
    );

    const fileInput = screen.getByLabelText(t("files.contextMenu.upload"));
    const file = new File(["x"], "test.zip");
    fireEvent.change(fileInput, { target: { files: [file] } });

    expect(
      await screen.findByText(t("files.error.serverMustBeStopped")),
    ).toBeInTheDocument();
  });

  it("maps server_not_stopped to the stop-the-server message on mkdir", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "server_not_stopped" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Right-click to open context menu and trigger new folder.
    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.newFolder") }),
    );
    fireEvent.change(screen.getByLabelText(t("files.folderName")), {
      target: { value: "mods" },
    });
    fireEvent.click(screen.getByRole("button", { name: t("files.create") }));

    expect(
      await screen.findByText(t("files.error.serverMustBeStopped")),
    ).toBeInTheDocument();
  });

  it("falls back to the generic message for other errors", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    mockApi.post.mockRejectedValue(new ApiError(500, undefined));
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Right-click to open context menu and trigger new folder.
    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.newFolder") }),
    );
    fireEvent.change(screen.getByLabelText(t("files.folderName")), {
      target: { value: "mods" },
    });
    fireEvent.click(screen.getByRole("button", { name: t("files.create") }));

    expect(
      await screen.findByText(t("files.error.generic")),
    ).toBeInTheDocument();
  });

  it("maps a 404 to the not-found message", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    mockApi.delete.mockRejectedValue(
      new ApiError(404, { reason: "not_found" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.delete") }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("files.delete.confirm") }),
    );

    expect(
      await screen.findByText(t("files.error.notFound")),
    ).toBeInTheDocument();
  });

  it("maps a 413 to the file-too-large message", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    mockPostFormWithProgress.mockRejectedValue(
      new ApiError(413, { reason: "file_too_large" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.upload") }),
    );
    const fileInput = screen.getByLabelText(t("files.contextMenu.upload"));
    const file = new File(["x"], "big.bin");
    fireEvent.change(fileInput, { target: { files: [file] } });

    expect(
      await screen.findByText(t("files.error.fileTooLarge")),
    ).toBeInTheDocument();
  });

  it("maps a 422 invalid_path to the invalid-path message", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    mockApi.post.mockRejectedValue(
      new ApiError(422, { reason: "invalid_path" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.newFolder") }),
    );
    fireEvent.change(screen.getByLabelText(t("files.folderName")), {
      target: { value: "../escape" },
    });
    fireEvent.click(screen.getByRole("button", { name: t("files.create") }));

    expect(
      await screen.findByText(t("files.error.invalidPath")),
    ).toBeInTheDocument();
  });

  it("maps a 422 symlink_refused to the symlink message", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    mockApi.delete.mockRejectedValue(
      new ApiError(422, { reason: "symlink_refused" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.delete") }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("files.delete.confirm") }),
    );

    expect(
      await screen.findByText(t("files.error.symlinkRefused")),
    ).toBeInTheDocument();
  });

  it("maps a 503 to the worker-unavailable message", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    mockApi.delete.mockRejectedValue(
      new ApiError(503, { reason: "worker_unavailable" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.delete") }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("files.delete.confirm") }),
    );

    expect(
      await screen.findByText(t("files.error.workerUnavailable")),
    ).toBeInTheDocument();
  });

  it("maps a 409 server_busy to the busy message", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "server_busy" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.newFolder") }),
    );
    fireEvent.change(screen.getByLabelText(t("files.folderName")), {
      target: { value: "test" },
    });
    fireEvent.click(screen.getByRole("button", { name: t("files.create") }));

    expect(
      await screen.findByText(t("files.error.serverBusy")),
    ).toBeInTheDocument();
  });

  it("shows a redirect notice with a link to #plugins on content_dir_protected (paper)", async () => {
    routeGet({
      detail: server({ server_type: "paper" }),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    mockPostFormWithProgress.mockRejectedValue(
      new ApiError(409, { reason: "content_dir_protected" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Right-click to open context menu and trigger upload.
    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.upload") }),
    );
    const fileInput = screen.getByLabelText(t("files.contextMenu.upload"));
    const file = new File(["x"], "test.jar");
    fireEvent.change(fileInput, { target: { files: [file] } });

    // The notice contains the tab noun and a link to #plugins.
    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent(t("serverDetail.tab.plugins"));
    const link = notice.querySelector("a[href='#plugins']");
    expect(link).toBeInTheDocument();
    expect(link).toHaveTextContent(t("serverDetail.tab.plugins"));
  });

  it("shows the mods tab noun in the redirect notice for a fabric server", async () => {
    routeGet({
      detail: server({ server_type: "fabric" }),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    mockPostFormWithProgress.mockRejectedValue(
      new ApiError(409, { reason: "content_dir_protected" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Right-click to open context menu and trigger upload.
    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.upload") }),
    );
    const fileInput = screen.getByLabelText(t("files.contextMenu.upload"));
    const file = new File(["x"], "test.jar");
    fireEvent.change(fileInput, { target: { files: [file] } });

    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent(t("serverDetail.tab.mods"));
  });
});

// Realistic mock that replicates browser DataTransfer quirks:
// 1. items/files are cleared after the synchronous event handler (seal()).
// 2. getAsFile() returns null after webkitGetAsEntry() on the same item.
class MockDataTransfer {
  private _items: Array<{
    kind: string;
    type: string;
    file: File;
    entryConsumed: boolean;
    entry: {
      isDirectory: boolean;
      isFile: boolean;
      name: string;
      createReader?: () => {
        readEntries: (cb: (entries: unknown[]) => void) => void;
      };
    } | null;
  }> = [];
  private _sealed = false;
  types: string[] = ["Files"];

  addFile(
    file: File,
    entry?: {
      isDirectory: boolean;
      isFile: boolean;
      name: string;
      createReader?: () => {
        readEntries: (cb: (entries: unknown[]) => void) => void;
      };
    },
  ) {
    this._items.push({
      kind: "file",
      type: file.type,
      file,
      entryConsumed: false,
      entry: entry ?? null,
    });
  }

  get items() {
    if (this._sealed) return [];
    return this._items.map((item) => ({
      kind: item.kind,
      type: item.type,
      getAsFile: () => {
        if (item.entryConsumed) return null;
        return this._sealed ? null : item.file;
      },
      webkitGetAsEntry: () => {
        item.entryConsumed = true;
        return item.entry;
      },
    }));
  }

  get files() {
    if (this._sealed) return [];
    return this._items.map((i) => i.file);
  }

  /** Simulate browser clearing DataTransfer after sync handler completes. */
  seal() {
    this._sealed = true;
  }
}

describe("ServerFilesTab drag-and-drop upload", () => {
  function dataTransfer(files: File[]): MockDataTransfer {
    const dt = new MockDataTransfer();
    for (const f of files) {
      dt.addFile(f, { isDirectory: false, isFile: true, name: f.name });
    }
    if (files.length === 0) dt.types = [];
    return dt;
  }

  it("shows a drop zone overlay when files are dragged over the listing", async () => {
    routeGet({ detail: server(), list: listing([]) });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const tree = document.querySelector(".file-tree") as HTMLElement;
    fireEvent.dragEnter(tree, { dataTransfer: dataTransfer([]) });

    expect(screen.getByText(t("files.dropZone"))).toBeInTheDocument();
  });

  it("hides the overlay when files are dragged away", async () => {
    routeGet({ detail: server(), list: listing([]) });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const tree = document.querySelector(".file-tree") as HTMLElement;
    fireEvent.dragEnter(tree, { dataTransfer: dataTransfer([]) });
    expect(screen.getByText(t("files.dropZone"))).toBeInTheDocument();

    fireEvent.dragLeave(tree, { dataTransfer: dataTransfer([]) });
    expect(screen.queryByText(t("files.dropZone"))).not.toBeInTheDocument();
  });

  it("uploads a dropped file to the current directory", async () => {
    routeGet({ detail: server(), list: listing([]) });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const tree = document.querySelector(".file-tree") as HTMLElement;
    const file = new File(["hello"], "readme.txt");
    fireEvent.drop(tree, { dataTransfer: dataTransfer([file]) });

    await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
    const [url, form] = mockPostFormWithProgress.mock.calls[0];
    expect(url).toBe(`${FILES_BASE}/upload?path=&extract=false`);
    expect((form as FormData).get("file")).toBe(file);
  });

  it("does not show overlay or upload when canEdit is false", async () => {
    mockCan = (code) => code !== "file:edit";
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    const tree = document.querySelector(".file-tree") as HTMLElement;
    fireEvent.dragEnter(tree, { dataTransfer: dataTransfer([]) });
    expect(screen.queryByText(t("files.dropZone"))).not.toBeInTheDocument();

    const file = new File(["x"], "bad.txt");
    fireEvent.drop(tree, { dataTransfer: dataTransfer([file]) });
    expect(mockPostFormWithProgress).not.toHaveBeenCalled();
  });

  it("does not show overlay or upload when server is running", async () => {
    routeGet({
      detail: server({ observed_state: "running", desired_state: "running" }),
      list: listing([]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.runningNotice"));

    const tree = document.querySelector(".file-tree") as HTMLElement;
    fireEvent.dragEnter(tree, { dataTransfer: dataTransfer([]) });
    expect(screen.queryByText(t("files.dropZone"))).not.toBeInTheDocument();

    const file = new File(["x"], "bad.txt");
    fireEvent.drop(tree, { dataTransfer: dataTransfer([file]) });
    expect(mockPostFormWithProgress).not.toHaveBeenCalled();
  });

  it("uses extract=false for dropped .zip files", async () => {
    routeGet({ detail: server(), list: listing([]) });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const tree = document.querySelector(".file-tree") as HTMLElement;
    const file = new File(["pk"], "world.zip");
    fireEvent.drop(tree, { dataTransfer: dataTransfer([file]) });

    await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
    const [url] = mockPostFormWithProgress.mock.calls[0];
    expect(url).toBe(`${FILES_BASE}/upload?path=&extract=false`);
  });

  it("uploads files from a dropped folder", async () => {
    routeGet({ detail: server(), list: listing([]) });
    mockApi.post.mockResolvedValue(undefined);
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const tree = document.querySelector(".file-tree") as HTMLElement;
    // Simulate a folder drop: webkitGetAsEntry returns a directory entry
    // with a createReader() that yields one file.
    const innerFile = new File(["hello"], "readme.txt");
    const folderDt = new MockDataTransfer();
    folderDt.addFile(new File([], ""), {
      isFile: false,
      isDirectory: true,
      name: "my-folder",
      createReader: () => {
        let read = false;
        return {
          readEntries: (cb: (entries: unknown[]) => void) => {
            if (!read) {
              read = true;
              cb([
                {
                  isFile: true,
                  isDirectory: false,
                  name: "readme.txt",
                  file: (resolve: (f: File) => void) => resolve(innerFile),
                },
              ]);
            } else {
              cb([]);
            }
          },
        };
      },
    });

    fireEvent.drop(tree, { dataTransfer: folderDt });

    // Directory creation is called first.
    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        expect.stringContaining("directories?path=my-folder"),
      ),
    );
    // Then the file is uploaded to the subdirectory.
    await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
    const [url] = mockPostFormWithProgress.mock.calls[0];
    expect(url).toBe(`${FILES_BASE}/upload?path=my-folder&extract=false`);
  });

  it("clears the overlay when dragend fires on the document", async () => {
    routeGet({ detail: server(), list: listing([]) });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const tree = document.querySelector(".file-tree") as HTMLElement;
    fireEvent.dragEnter(tree, { dataTransfer: dataTransfer([]) });
    expect(screen.getByText(t("files.dropZone"))).toBeInTheDocument();

    // Simulate the drag ending without a drop (e.g. Escape key during drag).
    fireEvent(document, new Event("dragend"));

    await waitFor(() =>
      expect(screen.queryByText(t("files.dropZone"))).not.toBeInTheDocument(),
    );
  });

  it("clears the overlay when drop fires on the document", async () => {
    routeGet({ detail: server(), list: listing([]) });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const tree = document.querySelector(".file-tree") as HTMLElement;
    fireEvent.dragEnter(tree, { dataTransfer: dataTransfer([]) });
    expect(screen.getByText(t("files.dropZone"))).toBeInTheDocument();

    // Any drop on the document resets the overlay.
    fireEvent(document, new Event("drop"));

    await waitFor(() =>
      expect(screen.queryByText(t("files.dropZone"))).not.toBeInTheDocument(),
    );
  });

  it("shows preparing indicator immediately on drop before upload starts", async () => {
    routeGet({ detail: server(), list: listing([]) });
    // Delay upload resolution so we can observe the preparing state.
    let resolveUpload: (() => void) | undefined;
    mockPostFormWithProgress.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveUpload = resolve;
        }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const tree = document.querySelector(".file-tree") as HTMLElement;
    const file = new File(["hello"], "readme.txt");
    fireEvent.drop(tree, { dataTransfer: dataTransfer([file]) });

    // The preparing indicator should appear before upload starts.
    await waitFor(() =>
      expect(screen.getByText(t("files.upload.preparing"))).toBeInTheDocument(),
    );

    // Wait for the upload mock to be invoked, then resolve it.
    await waitFor(() => expect(resolveUpload).toBeDefined());
    resolveUpload?.();
    await waitFor(() =>
      expect(
        screen.queryByText(t("files.upload.preparing")),
      ).not.toBeInTheDocument(),
    );
  });

  it("collects files synchronously before DataTransfer is cleared", async () => {
    routeGet({ detail: server(), list: listing([]) });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const tree = document.querySelector(".file-tree") as HTMLElement;
    const dt = new MockDataTransfer();
    dt.addFile(new File(["content"], "test.txt", { type: "text/plain" }), {
      isDirectory: false,
      isFile: true,
      name: "test.txt",
    });

    // Seal the DataTransfer after the synchronous event handler completes,
    // replicating browser behavior where items/files are cleared post-tick.
    fireEvent.drop(tree, { dataTransfer: dt });
    dt.seal();

    await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
    const [url, form] = mockPostFormWithProgress.mock.calls[0];
    expect(url).toBe(`${FILES_BASE}/upload?path=&extract=false`);
    expect((form as FormData).get("file")).toBeTruthy();
  });

  it("calls getAsFile before webkitGetAsEntry (item consumption)", async () => {
    routeGet({ detail: server(), list: listing([]) });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const tree = document.querySelector(".file-tree") as HTMLElement;
    const file = new File(["zip content"], "test.zip", {
      type: "application/zip",
    });
    const dt = new MockDataTransfer();
    dt.addFile(file, {
      isDirectory: false,
      isFile: true,
      name: "test.zip",
    });

    fireEvent.drop(tree, { dataTransfer: dt });

    // The upload should succeed even though webkitGetAsEntry consumes the item.
    await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
    const [, form] = mockPostFormWithProgress.mock.calls[0];
    expect((form as FormData).get("file")).toBeTruthy();
  });
});

describe("ServerFilesTab multi-select", () => {
  it("shows checkboxes on each file row when canEdit is true", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Each entry gets a checkbox labelled with its name.
    expect(screen.getByRole("checkbox", { name: "a.txt" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "b.txt" })).toBeInTheDocument();
  });

  it("hides checkboxes when canEdit is false", async () => {
    mockCan = (code) => code !== "file:edit";
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    expect(
      screen.queryByRole("checkbox", { name: "a.txt" }),
    ).not.toBeInTheDocument();
  });

  it("toggles individual selection on checkbox click", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    const checkA = screen.getByRole("checkbox", { name: "a.txt" });
    fireEvent.click(checkA);
    expect(checkA).toBeChecked();
    expect(
      screen.getByText(t("files.selectedCount", { count: 1 })),
    ).toBeInTheDocument();

    // Second click deselects.
    fireEvent.click(checkA);
    expect(checkA).not.toBeChecked();
  });

  it("selects a range with shift-click", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
        { name: "c.txt", is_dir: false },
        { name: "d.txt", is_dir: false },
      ]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Click first item normally.
    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));
    // Shift-click third item to select range [a, b, c].
    fireEvent.click(screen.getByRole("checkbox", { name: "c.txt" }), {
      shiftKey: true,
    });

    expect(screen.getByRole("checkbox", { name: "a.txt" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "b.txt" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "c.txt" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "d.txt" })).not.toBeChecked();
    expect(
      screen.getByText(t("files.selectedCount", { count: 3 })),
    ).toBeInTheDocument();
  });

  it("toggles individual items with ctrl-click", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "b.txt" }), {
      ctrlKey: true,
    });

    expect(screen.getByRole("checkbox", { name: "a.txt" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "b.txt" })).toBeChecked();

    // Ctrl-click again to deselect b.
    fireEvent.click(screen.getByRole("checkbox", { name: "b.txt" }), {
      ctrlKey: true,
    });
    expect(screen.getByRole("checkbox", { name: "b.txt" })).not.toBeChecked();
  });

  it("shows Select all button and selects all entries", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    const selectAllBtn = screen.getByRole("button", {
      name: t("files.selectAll"),
    });
    fireEvent.click(selectAllBtn);

    expect(screen.getByRole("checkbox", { name: "a.txt" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "b.txt" })).toBeChecked();
    expect(
      screen.getByText(t("files.selectedCount", { count: 2 })),
    ).toBeInTheDocument();

    // Button now says "Deselect all".
    const deselectBtn = screen.getByRole("button", {
      name: t("files.deselectAll"),
    });
    fireEvent.click(deselectBtn);

    expect(screen.getByRole("checkbox", { name: "a.txt" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "b.txt" })).not.toBeChecked();
  });

  it("clears selection on directory change", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("path=world")) {
        return Promise.resolve(listing([{ name: "level.dat", is_dir: false }]));
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(
          listing([
            { name: "world", is_dir: true },
            { name: "a.txt", is_dir: false },
          ]),
        );
      }
      return Promise.resolve(server());
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Select a file.
    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));
    expect(screen.getByRole("checkbox", { name: "a.txt" })).toBeChecked();

    // Navigate into a directory.
    fireEvent.click(screen.getByText(/world/));
    await screen.findByText(/level\.dat/);

    // The selection count should be gone.
    expect(
      screen.queryByText(t("files.selectedCount", { count: 1 })),
    ).not.toBeInTheDocument();
  });
});

describe("ServerFilesTab bulk operations", () => {
  it("shows bulk action buttons only when items are selected", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // No bulk buttons when nothing is selected.
    expect(
      screen.queryByRole("button", { name: t("files.bulk.delete") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("files.bulk.download") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("files.bulk.move") }),
    ).not.toBeInTheDocument();

    // Select an item.
    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));

    // Bulk buttons now visible.
    expect(
      screen.getByRole("button", { name: t("files.bulk.delete") }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: t("files.bulk.download") }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: t("files.bulk.move") }),
    ).toBeInTheDocument();
  });

  it("bulk deletes selected items after confirmation", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    mockApi.delete.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Select both items.
    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "b.txt" }), {
      ctrlKey: true,
    });

    // Click bulk delete.
    fireEvent.click(
      screen.getByRole("button", { name: t("files.bulk.delete") }),
    );
    // Confirmation dialog appears.
    expect(
      screen.getByText(t("files.bulk.delete.dialogBody", { count: 2 })),
    ).toBeInTheDocument();

    // Confirm.
    fireEvent.click(
      screen.getByRole("button", { name: t("files.bulk.delete.confirm") }),
    );

    await waitFor(() => expect(mockApi.delete).toHaveBeenCalledTimes(2));
    expect(mockApi.delete).toHaveBeenCalledWith(`${FILES_BASE}?path=a.txt`);
    expect(mockApi.delete).toHaveBeenCalledWith(`${FILES_BASE}?path=b.txt`);
  });

  it("bulk downloads selected files as a single ZIP", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Select both items.
    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "b.txt" }), {
      ctrlKey: true,
    });

    // Click bulk download.
    fireEvent.click(
      screen.getByRole("button", { name: t("files.bulk.download") }),
    );

    // Multiple files use fetchFileBlob (not downloadFile) to build a ZIP.
    await waitFor(() =>
      expect(mockDownload.fetchFileBlob).toHaveBeenCalledTimes(2),
    );
    expect(mockDownload.fetchFileBlob).toHaveBeenCalledWith(
      `${FILES_BASE}/download?path=a.txt`,
    );
    expect(mockDownload.fetchFileBlob).toHaveBeenCalledWith(
      `${FILES_BASE}/download?path=b.txt`,
    );
    // downloadFile should NOT have been called (ZIP handles both files).
    expect(mockDownload.downloadFile).not.toHaveBeenCalled();
  });

  it("bulk downloads a single file directly without ZIP", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Select only one item.
    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));

    // Click bulk download.
    fireEvent.click(
      screen.getByRole("button", { name: t("files.bulk.download") }),
    );

    // Single file uses downloadFile directly (no ZIP).
    await waitFor(() =>
      expect(mockDownload.downloadFile).toHaveBeenCalledTimes(1),
    );
    expect(mockDownload.downloadFile).toHaveBeenCalledWith(
      `${FILES_BASE}/download?path=a.txt`,
      "a.txt",
    );
    expect(mockDownload.fetchFileBlob).not.toHaveBeenCalled();
  });

  it("bulk moves selected items to a destination directory", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Select both items.
    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "b.txt" }), {
      ctrlKey: true,
    });

    // Click bulk move.
    fireEvent.click(screen.getByRole("button", { name: t("files.bulk.move") }));

    // The move dialog appears; enter destination.
    const input = screen.getByLabelText(t("files.bulk.move.destLabel"));
    fireEvent.change(input, { target: { value: "archive" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("files.bulk.move.confirm") }),
    );

    await waitFor(() => expect(mockApi.post).toHaveBeenCalledTimes(2));
    const calls = mockApi.post.mock.calls;
    expect(calls[0][0]).toBe(`${FILES_BASE}/rename`);
    expect(JSON.parse(calls[0][1].body)).toEqual({
      from: "a.txt",
      to: "archive/a.txt",
    });
    expect(calls[1][0]).toBe(`${FILES_BASE}/rename`);
    expect(JSON.parse(calls[1][1].body)).toEqual({
      from: "b.txt",
      to: "archive/b.txt",
    });
  });

  it("disables bulk delete and move when server is running", async () => {
    routeGet({
      detail: server({ observed_state: "running", desired_state: "running" }),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));

    const deleteBtn = screen.getByRole("button", {
      name: t("files.bulk.delete"),
    });
    const moveBtn = screen.getByRole("button", {
      name: t("files.bulk.move"),
    });
    const downloadBtn = screen.getByRole("button", {
      name: t("files.bulk.download"),
    });

    expect(deleteBtn).toBeDisabled();
    expect(moveBtn).toBeDisabled();
    // Download should still be enabled (read-only operation).
    expect(downloadBtn).not.toBeDisabled();
  });

  it("reports partial failure on bulk delete", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    mockApi.delete.mockImplementation((path: string) => {
      if (path.includes("a.txt")) return Promise.resolve(undefined);
      return Promise.reject(new ApiError(500, undefined));
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "b.txt" }), {
      ctrlKey: true,
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("files.bulk.delete") }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("files.bulk.delete.confirm") }),
    );

    await waitFor(() =>
      expect(
        screen.getByText(
          t("files.bulk.delete.partial", { done: 1, total: 2, failed: 1 }),
        ),
      ).toBeInTheDocument(),
    );
  });
});

describe("ServerFilesTab drag-and-drop file organization", () => {
  function internalDataTransfer(paths: string[]): DataTransfer {
    const data: Record<string, string> = {
      "application/x-file-move": JSON.stringify(paths),
    };
    return {
      types: ["application/x-file-move"],
      getData: (type: string) => data[type] ?? "",
      setData: (type: string, value: string) => {
        data[type] = value;
      },
      effectAllowed: "move",
      files: [] as unknown as FileList,
    } as unknown as DataTransfer;
  }

  it("moves a file into a folder on drop", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "world", is_dir: true },
        { name: "readme.txt", is_dir: false },
      ]),
    });
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/readme\.txt/);

    // Find the folder row (the <li> that contains "world").
    const folderBtn = screen.getByText(/world/).closest("li") as HTMLElement;
    const dt = internalDataTransfer(["readme.txt"]);

    fireEvent.dragOver(folderBtn, { dataTransfer: dt });
    fireEvent.drop(folderBtn, { dataTransfer: dt });

    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    const [url, init] = mockApi.post.mock.calls[0];
    expect(url).toBe(`${FILES_BASE}/rename`);
    expect(JSON.parse((init as { body: string }).body)).toEqual({
      from: "readme.txt",
      to: "world/readme.txt",
    });
  });

  it("moves a file to root via breadcrumb drop", async () => {
    // Start in the "config" subdirectory.
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("path=config") && path.includes("list=")) {
        return Promise.resolve(
          listing([{ name: "settings.yml", is_dir: false }]),
        );
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(listing([{ name: "config", is_dir: true }]));
      }
      return Promise.resolve(server());
    });
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await openFiles();

    // Navigate into "config".
    fireEvent.click(await screen.findByText(/config/));
    await screen.findByText(/settings\.yml/);

    // Drop settings.yml onto the root breadcrumb.
    const rootCrumb = screen.getByRole("button", { name: "survival" });
    const dt = internalDataTransfer(["config/settings.yml"]);

    fireEvent.dragOver(rootCrumb, { dataTransfer: dt });
    fireEvent.drop(rootCrumb, { dataTransfer: dt });

    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    // Find the rename call (not the search call, if any).
    const renameCalls = mockApi.post.mock.calls.filter(
      (call) => call[0] === `${FILES_BASE}/rename`,
    );
    expect(renameCalls.length).toBe(1);
    expect(JSON.parse(renameCalls[0][1].body)).toEqual({
      from: "config/settings.yml",
      to: "settings.yml",
    });
  });

  it("moves multiple selected items on drop", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "world", is_dir: true },
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Select both files.
    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "b.txt" }), {
      ctrlKey: true,
    });

    // Drop selected items onto the folder.
    const folderRow = screen.getByText(/world/).closest("li") as HTMLElement;
    const dt = internalDataTransfer(["a.txt", "b.txt"]);

    fireEvent.drop(folderRow, { dataTransfer: dt });

    await waitFor(() => expect(mockApi.post).toHaveBeenCalledTimes(2));
    const calls = mockApi.post.mock.calls;
    expect(JSON.parse(calls[0][1].body)).toEqual({
      from: "a.txt",
      to: "world/a.txt",
    });
    expect(JSON.parse(calls[1][1].body)).toEqual({
      from: "b.txt",
      to: "world/b.txt",
    });
  });

  it("makes no rename call when a folder is dropped onto itself", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "world", is_dir: true },
        { name: "readme.txt", is_dir: false },
      ]),
    });
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/world/);

    // Drag the "world" folder onto the "world" folder (self-drop). This would
    // otherwise compute a move of "world" to "world/world".
    const folderRow = screen.getByText(/world/).closest("li") as HTMLElement;
    const dt = internalDataTransfer(["world"]);

    fireEvent.dragOver(folderRow, { dataTransfer: dt });
    fireEvent.drop(folderRow, { dataTransfer: dt });

    // Let any async move work settle, then assert nothing happened: no rename
    // API call and no toast (neither the moved success nor a conflict error).
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockApi.post).not.toHaveBeenCalled();
    expect(screen.queryByText(t("files.moved"))).not.toBeInTheDocument();
  });

  it("skips the drop-target folder but moves the other selected items", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "world", is_dir: true },
        { name: "a.txt", is_dir: false },
      ]),
    });
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Select the drop-target folder and a file, then drop the selection onto
    // the folder: only the file should move, the folder's self-drop is skipped.
    fireEvent.click(screen.getByRole("checkbox", { name: "world" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }), {
      ctrlKey: true,
    });

    const folderRow = screen.getByText(/world/).closest("li") as HTMLElement;
    const dt = internalDataTransfer(["world", "a.txt"]);
    fireEvent.drop(folderRow, { dataTransfer: dt });

    await waitFor(() => expect(mockApi.post).toHaveBeenCalledTimes(1));
    expect(JSON.parse(mockApi.post.mock.calls[0][1].body)).toEqual({
      from: "a.txt",
      to: "world/a.txt",
    });
  });

  it("shows a conflict error on 409", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "world", is_dir: true },
        { name: "readme.txt", is_dir: false },
      ]),
    });
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "destination_exists" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(/readme\.txt/);

    const folderRow = screen.getByText(/world/).closest("li") as HTMLElement;
    const dt = internalDataTransfer(["readme.txt"]);

    fireEvent.drop(folderRow, { dataTransfer: dt });

    expect(
      await screen.findByText(
        t("files.error.moveConflict", { name: "readme.txt" }),
      ),
    ).toBeInTheDocument();
  });

  it("does not make rows draggable when canEdit is false", async () => {
    mockCan = (code) => code !== "file:edit";
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    expect(row).not.toHaveAttribute("draggable", "true");
  });

  it("does not make rows draggable when server is running", async () => {
    routeGet({
      detail: server({ observed_state: "running", desired_state: "running" }),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    const row = screen.getByText(/a\.txt/).closest("li") as HTMLElement;
    expect(row).not.toHaveAttribute("draggable", "true");
  });

  it("does not show upload overlay for internal drags", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    const tree = document.querySelector(".file-tree") as HTMLElement;
    const dt = internalDataTransfer(["a.txt"]);

    fireEvent.dragEnter(tree, { dataTransfer: dt });

    // The upload overlay should NOT appear for internal drags.
    expect(screen.queryByText(t("files.dropZone"))).not.toBeInTheDocument();
  });
});

// ── Context menu (issue #1465) ────────────────────────────────────────────────

describe("Context menu", () => {
  it("shows context menu on right-click with correct items for a file", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();

    const row = (await screen.findByText(/readme\.txt/)).closest(
      "li",
    ) as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    expect(
      screen.getByRole("menuitem", { name: t("files.contextMenu.open") }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: t("files.contextMenu.download") }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: t("files.contextMenu.rename") }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: t("files.contextMenu.delete") }),
    ).toBeInTheDocument();
  });

  it("shows 'Download as ZIP' for folders", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "world", is_dir: true }]),
    });
    renderPage();
    await openFiles();

    const row = (await screen.findByText(/world/)).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    expect(
      screen.getByRole("menuitem", {
        name: t("files.contextMenu.downloadZip"),
      }),
    ).toBeInTheDocument();
  });

  it("hides rename/delete when canEdit is false", async () => {
    mockCan = (code) => code !== "file:edit";
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();

    const row = (await screen.findByText(/readme\.txt/)).closest(
      "li",
    ) as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    expect(
      screen.getByRole("menuitem", { name: t("files.contextMenu.open") }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", { name: t("files.contextMenu.rename") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", { name: t("files.contextMenu.delete") }),
    ).not.toBeInTheDocument();
  });

  it("hides rename/delete when server is running", async () => {
    routeGet({
      detail: server({ observed_state: "running", desired_state: "running" }),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();

    const row = (await screen.findByText(/readme\.txt/)).closest(
      "li",
    ) as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    expect(
      screen.getByRole("menuitem", { name: t("files.contextMenu.open") }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", { name: t("files.contextMenu.rename") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", { name: t("files.contextMenu.delete") }),
    ).not.toBeInTheDocument();
  });

  it("dismisses context menu on click outside", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();

    const row = (await screen.findByText(/readme\.txt/)).closest(
      "li",
    ) as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    expect(screen.getByRole("menu")).toBeInTheDocument();

    // Click outside the menu.
    fireEvent.mouseDown(document.body);

    await waitFor(() =>
      expect(screen.queryByRole("menu")).not.toBeInTheDocument(),
    );
  });

  it("dismisses context menu on Escape", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();

    const row = (await screen.findByText(/readme\.txt/)).closest(
      "li",
    ) as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    expect(screen.getByRole("menu")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() =>
      expect(screen.queryByRole("menu")).not.toBeInTheDocument(),
    );
  });

  it("triggers delete when delete menu item is clicked", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    mockApi.delete.mockResolvedValue(undefined);
    renderPage();
    await openFiles();

    const row = (await screen.findByText(/readme\.txt/)).closest(
      "li",
    ) as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.delete") }),
    );

    // The delete confirmation dialog should appear.
    expect(
      await screen.findByText(t("files.delete.dialogTitle")),
    ).toBeInTheDocument();
  });

  it("triggers rename when rename menu item is clicked", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();

    const row = (await screen.findByText(/readme\.txt/)).closest(
      "li",
    ) as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.rename") }),
    );

    // The rename dialog should appear.
    expect(await screen.findByText(t("files.newName"))).toBeInTheDocument();
  });

  it("triggers download when download menu item is clicked", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    mockDownload.downloadFile.mockResolvedValue(undefined);
    renderPage();
    await openFiles();

    const row = (await screen.findByText(/readme\.txt/)).closest(
      "li",
    ) as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });

    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.download") }),
    );

    await waitFor(() => expect(mockDownload.downloadFile).toHaveBeenCalled());
  });
});

// ── Keyboard shortcuts (issue #1465) ──────────────────────────────────────────

describe("Keyboard shortcuts", () => {
  it("Ctrl+A selects all items", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    fireEvent.keyDown(document, { key: "a", ctrlKey: true });

    // Both checkboxes should be checked.
    await waitFor(() => {
      expect(screen.getByRole("checkbox", { name: "a.txt" })).toBeChecked();
      expect(screen.getByRole("checkbox", { name: "b.txt" })).toBeChecked();
    });
  });

  it("Cmd+A (meta) selects all items", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    fireEvent.keyDown(document, { key: "a", metaKey: true });

    await waitFor(() => {
      expect(screen.getByRole("checkbox", { name: "a.txt" })).toBeChecked();
      expect(screen.getByRole("checkbox", { name: "b.txt" })).toBeChecked();
    });
  });

  it("Escape clears selection", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Select the file first.
    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));
    expect(screen.getByRole("checkbox", { name: "a.txt" })).toBeChecked();

    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() =>
      expect(screen.getByRole("checkbox", { name: "a.txt" })).not.toBeChecked(),
    );
  });

  it("Delete opens delete confirmation for selected item", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Select the file first.
    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));

    fireEvent.keyDown(document, { key: "Delete" });

    expect(
      await screen.findByText(t("files.delete.dialogTitle")),
    ).toBeInTheDocument();
  });

  it("Backspace opens delete confirmation for selected item", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));

    fireEvent.keyDown(document, { key: "Backspace" });

    expect(
      await screen.findByText(t("files.delete.dialogTitle")),
    ).toBeInTheDocument();
  });

  it("Delete does nothing when no items are selected", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    fireEvent.keyDown(document, { key: "Delete" });

    expect(
      screen.queryByText(t("files.delete.dialogTitle")),
    ).not.toBeInTheDocument();
  });

  it("Delete does nothing when server is running", async () => {
    routeGet({
      detail: server({ observed_state: "running", desired_state: "running" }),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Can't select (checkboxes hidden when running for this test -- actually
    // canEdit is still true, just running). Let me check.
    // Actually checkboxes appear when canEdit is true regardless of running.
    // So we can still select.
    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));

    fireEvent.keyDown(document, { key: "Delete" });

    // No dialog because server is not at rest.
    expect(
      screen.queryByText(t("files.delete.dialogTitle")),
    ).not.toBeInTheDocument();
  });

  it("F2 opens rename dialog for single selected item", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/readme\.txt/);

    fireEvent.click(screen.getByRole("checkbox", { name: "readme.txt" }));

    fireEvent.keyDown(document, { key: "F2" });

    expect(await screen.findByText(t("files.newName"))).toBeInTheDocument();
  });

  it("F2 does nothing when multiple items are selected", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Select both items.
    fireEvent.keyDown(document, { key: "a", ctrlKey: true });

    fireEvent.keyDown(document, { key: "F2" });

    // No rename dialog.
    expect(screen.queryByText(t("files.newName"))).not.toBeInTheDocument();
  });

  it("keyboard shortcuts are suppressed when typing in an input", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    // Focus the search input.
    const searchInput = screen.getByRole("searchbox");

    // Fire Ctrl+A on the input — should NOT select all files.
    fireEvent.keyDown(searchInput, { key: "a", ctrlKey: true });

    expect(screen.getByRole("checkbox", { name: "a.txt" })).not.toBeChecked();
  });

  it("Escape on context menu dismisses menu but preserves selection", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/readme\.txt/);

    // Select the file.
    fireEvent.click(screen.getByRole("checkbox", { name: "readme.txt" }));
    expect(screen.getByRole("checkbox", { name: "readme.txt" })).toBeChecked();

    // Open context menu.
    const row = screen.getByText(/readme\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    expect(screen.getByRole("menu")).toBeInTheDocument();

    // Press Escape — should dismiss menu but keep selection.
    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() =>
      expect(screen.queryByRole("menu")).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("checkbox", { name: "readme.txt" })).toBeChecked();
  });
});

// ── Navigation history (issue #1475) ────────────────────────────────────────

describe("ServerFilesTab navigation history", () => {
  it("shows back and forward buttons that are initially disabled", async () => {
    routeGet({ detail: server(), list: listing([]) });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const backBtn = screen.getByRole("button", { name: t("files.nav.back") });
    const fwdBtn = screen.getByRole("button", {
      name: t("files.nav.forward"),
    });
    expect(backBtn).toBeDisabled();
    expect(fwdBtn).toBeDisabled();
  });

  it("enables back after navigating into a directory", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("path=world") && path.includes("list=")) {
        return Promise.resolve(listing([{ name: "level.dat", is_dir: false }]));
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(listing([{ name: "world", is_dir: true }]));
      }
      return Promise.resolve(server());
    });
    renderPage();
    await openFiles();

    // Navigate into world.
    fireEvent.click(await screen.findByText(/world/));
    await screen.findByText(/level\.dat/);

    const backBtn = screen.getByRole("button", { name: t("files.nav.back") });
    expect(backBtn).not.toBeDisabled();
  });

  it("goes back to root and then forward to the previous directory", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("path=world") && path.includes("list=")) {
        return Promise.resolve(listing([{ name: "level.dat", is_dir: false }]));
      }
      if (path.includes("/files?path=") && path.includes("list=")) {
        return Promise.resolve(listing([{ name: "world", is_dir: true }]));
      }
      return Promise.resolve(server());
    });
    renderPage();
    await openFiles();

    // Navigate into world.
    fireEvent.click(await screen.findByText(/world/));
    await screen.findByText(/level\.dat/);

    // Go back.
    fireEvent.click(screen.getByRole("button", { name: t("files.nav.back") }));
    // Should be back at root listing.
    await screen.findByText(/world/);

    // Forward button should now be enabled.
    const fwdBtn = screen.getByRole("button", {
      name: t("files.nav.forward"),
    });
    expect(fwdBtn).not.toBeDisabled();

    // Go forward.
    fireEvent.click(fwdBtn);
    await waitFor(() =>
      expect(mockApi.get).toHaveBeenCalledWith(
        `${FILES_BASE}?path=world&list=true`,
      ),
    );
  });

  it("clears forward stack when navigating to a new location after going back", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("path=config") && path.includes("list=")) {
        return Promise.resolve(listing([{ name: "cfg.yml", is_dir: false }]));
      }
      if (path.includes("path=world") && path.includes("list=")) {
        return Promise.resolve(listing([{ name: "level.dat", is_dir: false }]));
      }
      if (path.includes("/files?path=") && path.includes("list=")) {
        return Promise.resolve(
          listing([
            { name: "world", is_dir: true },
            { name: "config", is_dir: true },
          ]),
        );
      }
      return Promise.resolve(server());
    });
    renderPage();
    await openFiles();

    // Navigate into world.
    fireEvent.click(await screen.findByText(/world/));
    await screen.findByText(/level\.dat/);

    // Go back to root.
    fireEvent.click(screen.getByRole("button", { name: t("files.nav.back") }));
    await screen.findByText(/config/);

    // Navigate into config (new path, should clear forward stack).
    fireEvent.click(screen.getByText(/config/));
    await screen.findByText(/cfg\.yml/);

    // Forward should be disabled now.
    const fwdBtn = screen.getByRole("button", {
      name: t("files.nav.forward"),
    });
    expect(fwdBtn).toBeDisabled();
  });
});

// ── Unsaved changes guard (issue #1486) ────────────────────────────────────

describe("ServerFilesTab unsaved changes guard", () => {
  it("shows discard dialog when editing a file and clicking a directory", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return Promise.resolve({
          path: "server.properties",
          content_base64: encodeUtf8Base64("motd=hello\n"),
        });
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(
          listing([
            { name: "world", is_dir: true },
            { name: "server.properties", is_dir: false },
          ]),
        );
      }
      return Promise.resolve(server());
    });
    renderPage();
    await openFiles();

    // Open the file.
    fireEvent.click(await screen.findByText(/server\.properties/));
    const editor = (await screen.findByLabelText(
      t("files.editorLabel"),
    )) as HTMLTextAreaElement;

    // Edit the file (create a draft).
    fireEvent.change(editor, { target: { value: "motd=changed\n" } });

    // Click the directory to navigate away.
    fireEvent.click(screen.getByText(/world/));

    // The discard dialog should appear.
    expect(
      await screen.findByText(t("files.unsaved.title")),
    ).toBeInTheDocument();
    expect(screen.getByText(t("files.unsaved.body"))).toBeInTheDocument();
  });

  it("navigates on confirm and discards the draft", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("path=world") && path.includes("list=")) {
        return Promise.resolve(listing([{ name: "level.dat", is_dir: false }]));
      }
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return Promise.resolve({
          path: "server.properties",
          content_base64: encodeUtf8Base64("motd=hello\n"),
        });
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(
          listing([
            { name: "world", is_dir: true },
            { name: "server.properties", is_dir: false },
          ]),
        );
      }
      return Promise.resolve(server());
    });
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/server\.properties/));
    const editor = await screen.findByLabelText(t("files.editorLabel"));
    fireEvent.change(editor, { target: { value: "motd=changed\n" } });

    // Try to navigate to the directory.
    fireEvent.click(screen.getByText(/world/));
    await screen.findByText(t("files.unsaved.title"));

    // Confirm discard.
    fireEvent.click(
      screen.getByRole("button", { name: t("files.unsaved.discard") }),
    );

    // Should navigate into "world".
    await waitFor(() =>
      expect(mockApi.get).toHaveBeenCalledWith(
        `${FILES_BASE}?path=world&list=true`,
      ),
    );
    // The discard dialog should be gone.
    expect(
      screen.queryByText(t("files.unsaved.title")),
    ).not.toBeInTheDocument();
  });

  it("keeps the file open on cancel", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return Promise.resolve({
          path: "server.properties",
          content_base64: encodeUtf8Base64("motd=hello\n"),
        });
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(
          listing([
            { name: "world", is_dir: true },
            { name: "server.properties", is_dir: false },
          ]),
        );
      }
      return Promise.resolve(server());
    });
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/server\.properties/));
    const editor = await screen.findByLabelText(t("files.editorLabel"));
    fireEvent.change(editor, { target: { value: "motd=changed\n" } });

    fireEvent.click(screen.getByText(/world/));
    await screen.findByText(t("files.unsaved.title"));

    // Cancel — click the close/cancel button on the dialog.
    fireEvent.click(screen.getByRole("button", { name: t("common.cancel") }));

    // The dialog should close and the editor should still be visible.
    await waitFor(() =>
      expect(
        screen.queryByText(t("files.unsaved.title")),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByLabelText(t("files.editorLabel"))).toBeInTheDocument();
  });

  it("does not show dialog when there are no unsaved changes", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("path=world") && path.includes("list=")) {
        return Promise.resolve(listing([{ name: "level.dat", is_dir: false }]));
      }
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return Promise.resolve({
          path: "server.properties",
          content_base64: encodeUtf8Base64("motd=hello\n"),
        });
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(
          listing([
            { name: "world", is_dir: true },
            { name: "server.properties", is_dir: false },
          ]),
        );
      }
      return Promise.resolve(server());
    });
    renderPage();
    await openFiles();

    // Open the file but don't edit it.
    fireEvent.click(await screen.findByText(/server\.properties/));
    await screen.findByLabelText(t("files.editorLabel"));

    // Click the directory.
    fireEvent.click(screen.getByText(/world/));

    // No discard dialog — navigates directly.
    await waitFor(() =>
      expect(mockApi.get).toHaveBeenCalledWith(
        `${FILES_BASE}?path=world&list=true`,
      ),
    );
    expect(
      screen.queryByText(t("files.unsaved.title")),
    ).not.toBeInTheDocument();
  });

  it("saving clears the guard so no dialog appears", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("path=world") && path.includes("list=")) {
        return Promise.resolve(listing([{ name: "level.dat", is_dir: false }]));
      }
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return Promise.resolve({
          path: "server.properties",
          content_base64: encodeUtf8Base64("motd=hello\n"),
        });
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(
          listing([
            { name: "world", is_dir: true },
            { name: "server.properties", is_dir: false },
          ]),
        );
      }
      return Promise.resolve(server());
    });
    mockApi.put.mockResolvedValue(undefined);
    renderPage();
    await openFiles();

    // Open and edit the file.
    fireEvent.click(await screen.findByText(/server\.properties/));
    const editor = await screen.findByLabelText(t("files.editorLabel"));
    fireEvent.change(editor, { target: { value: "motd=changed\n" } });

    // Save the file.
    fireEvent.click(screen.getByRole("button", { name: t("files.save") }));
    await waitFor(() => expect(mockApi.put).toHaveBeenCalled());

    // Navigate away — no dialog should appear.
    fireEvent.click(screen.getByText(/world/));
    await waitFor(() =>
      expect(mockApi.get).toHaveBeenCalledWith(
        `${FILES_BASE}?path=world&list=true`,
      ),
    );
    expect(
      screen.queryByText(t("files.unsaved.title")),
    ).not.toBeInTheDocument();
  });
});

// ── Overwrite confirmation dialog ─────────────────────────────────────────────

describe("ServerFilesTab overwrite confirmation", () => {
  it("shows overwrite dialog when uploading a file that already exists via context menu", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/readme\.txt/);

    // Right-click to open context menu and trigger upload.
    const row = screen.getByText(/readme\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.upload") }),
    );

    // Choose a file with the same name as an existing file.
    const file = new File(["new content"], "readme.txt");
    fireEvent.change(screen.getByLabelText(t("files.contextMenu.upload")), {
      target: { files: [file] },
    });

    // The overwrite dialog should appear.
    expect(
      await screen.findByText(t("files.overwrite.title")),
    ).toBeInTheDocument();
    expect(
      screen.getByText(t("files.overwrite.body", { name: "readme.txt" })),
    ).toBeInTheDocument();
  });

  it("uploads the file when user clicks overwrite", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/readme\.txt/);

    const row = screen.getByText(/readme\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.upload") }),
    );

    const file = new File(["new"], "readme.txt");
    fireEvent.change(screen.getByLabelText(t("files.contextMenu.upload")), {
      target: { files: [file] },
    });

    await screen.findByText(t("files.overwrite.title"));
    fireEvent.click(
      screen.getByRole("button", { name: t("files.overwrite.overwrite") }),
    );

    await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
  });

  it("does not upload the file when user clicks skip", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/readme\.txt/);

    const row = screen.getByText(/readme\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.upload") }),
    );

    const file = new File(["new"], "readme.txt");
    fireEvent.change(screen.getByLabelText(t("files.contextMenu.upload")), {
      target: { files: [file] },
    });

    await screen.findByText(t("files.overwrite.title"));
    fireEvent.click(
      screen.getByRole("button", { name: t("files.overwrite.skip") }),
    );

    // Dialog should close and no upload should occur.
    await waitFor(() =>
      expect(
        screen.queryByText(t("files.overwrite.title")),
      ).not.toBeInTheDocument(),
    );
    expect(mockPostFormWithProgress).not.toHaveBeenCalled();
  });

  it("does not upload the file when user clicks cancel", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/readme\.txt/);

    const row = screen.getByText(/readme\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.upload") }),
    );

    const file = new File(["new"], "readme.txt");
    fireEvent.change(screen.getByLabelText(t("files.contextMenu.upload")), {
      target: { files: [file] },
    });

    await screen.findByText(t("files.overwrite.title"));
    fireEvent.click(screen.getByRole("button", { name: t("common.cancel") }));

    await waitFor(() =>
      expect(
        screen.queryByText(t("files.overwrite.title")),
      ).not.toBeInTheDocument(),
    );
    expect(mockPostFormWithProgress).not.toHaveBeenCalled();
  });

  it("does not show dialog when uploading a file with a new name via context menu", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "existing.txt", is_dir: false }]),
    });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/existing\.txt/);

    const row = screen.getByText(/existing\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.upload") }),
    );

    // Upload a file with a different name — no conflict.
    const file = new File(["content"], "brand-new.txt");
    fireEvent.change(screen.getByLabelText(t("files.contextMenu.upload")), {
      target: { files: [file] },
    });

    // Upload should proceed without a dialog.
    await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
    expect(
      screen.queryByText(t("files.overwrite.title")),
    ).not.toBeInTheDocument();
  });

  it("does not show dialog for directories with the same name", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "world", is_dir: true }]),
    });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/world/);

    const row = screen.getByText(/world/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.upload") }),
    );

    // Upload a file named "world" — only files conflict, not directories.
    const file = new File(["content"], "world");
    fireEvent.change(screen.getByLabelText(t("files.contextMenu.upload")), {
      target: { files: [file] },
    });

    await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
    expect(
      screen.queryByText(t("files.overwrite.title")),
    ).not.toBeInTheDocument();
  });

  describe("drag-and-drop overwrite", () => {
    function dataTransfer(files: File[]): DataTransfer {
      return {
        files,
        types: files.length > 0 ? ["Files"] : [],
      } as unknown as DataTransfer;
    }

    it("shows overwrite dialog when dropping a file that already exists", async () => {
      routeGet({
        detail: server(),
        list: listing([{ name: "readme.txt", is_dir: false }]),
      });
      mockPostFormWithProgress.mockResolvedValue(undefined);
      renderPage();
      await openFiles();
      await screen.findByText(/readme\.txt/);

      const tree = document.querySelector(".file-tree") as HTMLElement;
      const file = new File(["new content"], "readme.txt");
      fireEvent.drop(tree, { dataTransfer: dataTransfer([file]) });

      // The overwrite dialog should appear.
      expect(
        await screen.findByText(t("files.overwrite.title")),
      ).toBeInTheDocument();
    });

    it("uploads on overwrite click in drop", async () => {
      routeGet({
        detail: server(),
        list: listing([{ name: "readme.txt", is_dir: false }]),
      });
      mockPostFormWithProgress.mockResolvedValue(undefined);
      renderPage();
      await openFiles();
      await screen.findByText(/readme\.txt/);

      const tree = document.querySelector(".file-tree") as HTMLElement;
      const file = new File(["new content"], "readme.txt");
      fireEvent.drop(tree, { dataTransfer: dataTransfer([file]) });

      await screen.findByText(t("files.overwrite.title"));
      fireEvent.click(
        screen.getByRole("button", { name: t("files.overwrite.overwrite") }),
      );

      await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
    });

    it("skips a conflicting file on skip click in drop", async () => {
      routeGet({
        detail: server(),
        list: listing([{ name: "readme.txt", is_dir: false }]),
      });
      mockPostFormWithProgress.mockResolvedValue(undefined);
      renderPage();
      await openFiles();
      await screen.findByText(/readme\.txt/);

      const tree = document.querySelector(".file-tree") as HTMLElement;
      const file = new File(["new content"], "readme.txt");
      fireEvent.drop(tree, { dataTransfer: dataTransfer([file]) });

      await screen.findByText(t("files.overwrite.title"));
      fireEvent.click(
        screen.getByRole("button", { name: t("files.overwrite.skip") }),
      );

      // No upload should occur — the only file was skipped.
      await waitFor(() =>
        expect(
          screen.queryByText(t("files.overwrite.title")),
        ).not.toBeInTheDocument(),
      );
      expect(mockPostFormWithProgress).not.toHaveBeenCalled();
    });

    it("shows apply-all checkbox when multiple files conflict on drop", async () => {
      routeGet({
        detail: server(),
        list: listing([
          { name: "a.txt", is_dir: false },
          { name: "b.txt", is_dir: false },
        ]),
      });
      mockPostFormWithProgress.mockResolvedValue(undefined);
      renderPage();
      await openFiles();
      await screen.findByText(/a\.txt/);

      const tree = document.querySelector(".file-tree") as HTMLElement;
      const fileA = new File(["new a"], "a.txt");
      const fileB = new File(["new b"], "b.txt");
      fireEvent.drop(tree, { dataTransfer: dataTransfer([fileA, fileB]) });

      await screen.findByText(t("files.overwrite.title"));
      // The apply-all checkbox should be visible.
      expect(
        screen.getByText(t("files.overwrite.applyAll")),
      ).toBeInTheDocument();
    });

    it("does not show apply-all checkbox for a single conflicting file on drop", async () => {
      routeGet({
        detail: server(),
        list: listing([
          { name: "a.txt", is_dir: false },
          { name: "new.txt", is_dir: false },
        ]),
      });
      mockPostFormWithProgress.mockResolvedValue(undefined);
      renderPage();
      await openFiles();
      await screen.findByText(/a\.txt/);

      const tree = document.querySelector(".file-tree") as HTMLElement;
      // Only a.txt conflicts; new-file.txt does not.
      const fileA = new File(["new a"], "a.txt");
      const fileB = new File(["new b"], "new-file.txt");
      fireEvent.drop(tree, { dataTransfer: dataTransfer([fileA, fileB]) });

      await screen.findByText(t("files.overwrite.title"));
      expect(
        screen.queryByText(t("files.overwrite.applyAll")),
      ).not.toBeInTheDocument();
    });

    it("overwrite-all skips remaining dialogs and uploads all files", async () => {
      routeGet({
        detail: server(),
        list: listing([
          { name: "a.txt", is_dir: false },
          { name: "b.txt", is_dir: false },
        ]),
      });
      mockPostFormWithProgress.mockResolvedValue(undefined);
      renderPage();
      await openFiles();
      await screen.findByText(/a\.txt/);

      const tree = document.querySelector(".file-tree") as HTMLElement;
      const fileA = new File(["new a"], "a.txt");
      const fileB = new File(["new b"], "b.txt");
      fireEvent.drop(tree, { dataTransfer: dataTransfer([fileA, fileB]) });

      // First dialog for a.txt.
      await screen.findByText(t("files.overwrite.title"));
      // Check the apply-all checkbox.
      fireEvent.click(screen.getByText(t("files.overwrite.applyAll")));
      fireEvent.click(
        screen.getByRole("button", { name: t("files.overwrite.overwrite") }),
      );

      // Should upload both files without another dialog.
      await waitFor(() =>
        expect(mockPostFormWithProgress).toHaveBeenCalledTimes(2),
      );
    });

    it("skip-all skips remaining conflicting files on drop", async () => {
      routeGet({
        detail: server(),
        list: listing([
          { name: "a.txt", is_dir: false },
          { name: "b.txt", is_dir: false },
        ]),
      });
      mockPostFormWithProgress.mockResolvedValue(undefined);
      renderPage();
      await openFiles();
      await screen.findByText(/a\.txt/);

      const tree = document.querySelector(".file-tree") as HTMLElement;
      const fileA = new File(["new a"], "a.txt");
      const fileB = new File(["new b"], "b.txt");
      fireEvent.drop(tree, { dataTransfer: dataTransfer([fileA, fileB]) });

      // First dialog for a.txt.
      await screen.findByText(t("files.overwrite.title"));
      // Check the apply-all checkbox and click skip.
      fireEvent.click(screen.getByText(t("files.overwrite.applyAll")));
      fireEvent.click(
        screen.getByRole("button", { name: t("files.overwrite.skip") }),
      );

      // Both files should be skipped — no upload.
      await waitFor(() =>
        expect(
          screen.queryByText(t("files.overwrite.title")),
        ).not.toBeInTheDocument(),
      );
      expect(mockPostFormWithProgress).not.toHaveBeenCalled();
    });

    it("drops non-conflicting files without a dialog", async () => {
      routeGet({
        detail: server(),
        list: listing([{ name: "existing.txt", is_dir: false }]),
      });
      mockPostFormWithProgress.mockResolvedValue(undefined);
      renderPage();
      await openFiles();
      await screen.findByText(/existing\.txt/);

      const tree = document.querySelector(".file-tree") as HTMLElement;
      const file = new File(["content"], "brand-new.txt");
      fireEvent.drop(tree, { dataTransfer: dataTransfer([file]) });

      // No dialog — upload proceeds immediately.
      await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
      expect(
        screen.queryByText(t("files.overwrite.title")),
      ).not.toBeInTheDocument();
    });

    it("detects conflicts using the listing snapshot from before the yield", async () => {
      // Regression: the overwrite check must use the listing entries
      // captured synchronously before the setTimeout(0) yield. If it
      // reads the ref after the yield, an intervening re-render (e.g.
      // triggered by a React Query background refetch) could clear the
      // entries and silently skip the overwrite dialog.
      routeGet({
        detail: server(),
        list: listing([{ name: "readme.txt", is_dir: false }]),
      });
      mockPostFormWithProgress.mockResolvedValue(undefined);
      renderPage();
      await openFiles();
      await screen.findByText(/readme\.txt/);

      // Simulate the listing becoming empty between the drop and the
      // overwrite check (e.g. a background refetch returns an empty
      // response or the query is invalidated). The setTimeout(0) yield
      // gives React a chance to re-render with the new data.
      mockApi.get.mockImplementation((path: string) => {
        if (path.includes("/files?path=") && path.includes("list=")) {
          return Promise.resolve(listing([]));
        }
        return Promise.resolve(server());
      });

      const tree = document.querySelector(".file-tree") as HTMLElement;
      const file = new File(["new content"], "readme.txt");
      fireEvent.drop(tree, { dataTransfer: dataTransfer([file]) });

      // The overwrite dialog should still appear because the snapshot was
      // taken before the yield.
      expect(
        await screen.findByText(t("files.overwrite.title")),
      ).toBeInTheDocument();
    });

    it("cancel stops the entire upload on drop", async () => {
      routeGet({
        detail: server(),
        list: listing([
          { name: "a.txt", is_dir: false },
          { name: "b.txt", is_dir: false },
        ]),
      });
      mockPostFormWithProgress.mockResolvedValue(undefined);
      renderPage();
      await openFiles();
      await screen.findByText(/a\.txt/);

      const tree = document.querySelector(".file-tree") as HTMLElement;
      const fileA = new File(["new a"], "a.txt");
      const fileB = new File(["new b"], "b.txt");
      fireEvent.drop(tree, { dataTransfer: dataTransfer([fileA, fileB]) });

      await screen.findByText(t("files.overwrite.title"));
      fireEvent.click(screen.getByRole("button", { name: t("common.cancel") }));

      // No upload at all.
      await waitFor(() =>
        expect(
          screen.queryByText(t("files.overwrite.title")),
        ).not.toBeInTheDocument(),
      );
      expect(mockPostFormWithProgress).not.toHaveBeenCalled();
    });

    it("shows overwrite dialog when dropping a folder that already exists as a directory", async () => {
      routeGet({
        detail: server(),
        list: listing([{ name: "myfolder", is_dir: true }]),
      });
      mockApi.post.mockResolvedValue(undefined);
      mockPostFormWithProgress.mockResolvedValue(undefined);
      renderPage();
      await openFiles();
      await screen.findByText(/myfolder/);

      const tree = document.querySelector(".file-tree") as HTMLElement;
      const innerFile = new File(["hello"], "readme.txt");
      const folderDt = new MockDataTransfer();
      folderDt.addFile(new File([], ""), {
        isFile: false,
        isDirectory: true,
        name: "myfolder",
        createReader: () => {
          let read = false;
          return {
            readEntries: (cb: (entries: unknown[]) => void) => {
              if (!read) {
                read = true;
                cb([
                  {
                    isFile: true,
                    isDirectory: false,
                    name: "readme.txt",
                    file: (resolve: (f: File) => void) => resolve(innerFile),
                  },
                ]);
              } else {
                cb([]);
              }
            },
          };
        },
      });

      fireEvent.drop(tree, { dataTransfer: folderDt });

      // The folder-level overwrite dialog should appear.
      expect(
        await screen.findByText(t("files.overwrite.title")),
      ).toBeInTheDocument();
    });

    it("skips folder files when user clicks skip on folder overwrite", async () => {
      routeGet({
        detail: server(),
        list: listing([{ name: "myfolder", is_dir: true }]),
      });
      mockApi.post.mockResolvedValue(undefined);
      mockPostFormWithProgress.mockResolvedValue(undefined);
      renderPage();
      await openFiles();
      await screen.findByText(/myfolder/);

      const tree = document.querySelector(".file-tree") as HTMLElement;
      const innerFile = new File(["hello"], "readme.txt");
      const folderDt = new MockDataTransfer();
      folderDt.addFile(new File([], ""), {
        isFile: false,
        isDirectory: true,
        name: "myfolder",
        createReader: () => {
          let read = false;
          return {
            readEntries: (cb: (entries: unknown[]) => void) => {
              if (!read) {
                read = true;
                cb([
                  {
                    isFile: true,
                    isDirectory: false,
                    name: "readme.txt",
                    file: (resolve: (f: File) => void) => resolve(innerFile),
                  },
                ]);
              } else {
                cb([]);
              }
            },
          };
        },
      });

      fireEvent.drop(tree, { dataTransfer: folderDt });

      await screen.findByText(t("files.overwrite.title"));
      fireEvent.click(
        screen.getByRole("button", { name: t("files.overwrite.skip") }),
      );

      // All folder files skipped — no upload.
      await waitFor(() =>
        expect(
          screen.queryByText(t("files.overwrite.title")),
        ).not.toBeInTheDocument(),
      );
      expect(mockPostFormWithProgress).not.toHaveBeenCalled();
    });

    it("uploads folder files when user clicks overwrite on folder overwrite", async () => {
      routeGet({
        detail: server(),
        list: listing([{ name: "myfolder", is_dir: true }]),
      });
      mockApi.post.mockResolvedValue(undefined);
      mockPostFormWithProgress.mockResolvedValue(undefined);
      renderPage();
      await openFiles();
      await screen.findByText(/myfolder/);

      const tree = document.querySelector(".file-tree") as HTMLElement;
      const innerFile = new File(["hello"], "readme.txt");
      const folderDt = new MockDataTransfer();
      folderDt.addFile(new File([], ""), {
        isFile: false,
        isDirectory: true,
        name: "myfolder",
        createReader: () => {
          let read = false;
          return {
            readEntries: (cb: (entries: unknown[]) => void) => {
              if (!read) {
                read = true;
                cb([
                  {
                    isFile: true,
                    isDirectory: false,
                    name: "readme.txt",
                    file: (resolve: (f: File) => void) => resolve(innerFile),
                  },
                ]);
              } else {
                cb([]);
              }
            },
          };
        },
      });

      fireEvent.drop(tree, { dataTransfer: folderDt });

      await screen.findByText(t("files.overwrite.title"));
      fireEvent.click(
        screen.getByRole("button", { name: t("files.overwrite.overwrite") }),
      );

      // File should be uploaded.
      await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
    });
  });
});
