// @vitest-environment jsdom
// Pinned to jsdom: the drag-and-drop tests rely on jsdom's DataTransfer /
// DataTransferItem / webkitGetAsEntry behavior, which happy-dom implements
// differently so drop handlers never fire (issue #1751).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { unzipSync } from "fflate";
import type { ComponentProps } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  type MockInstance,
  vi,
} from "vitest";
import { ApiError } from "../api/client.ts";
import { DownloadTooLargeError } from "../api/download.ts";
import { setAccessToken } from "../auth/tokenStore.ts";
import { ToastProvider } from "../components/Toast.tsx";
import { humanizeBytes } from "../format.ts";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { installMockWebSocket } from "../test/mockWebSocket.ts";
import { encodeUtf8Base64 } from "./fileText.ts";
import { ServerDetailPage } from "./ServerDetailPage.tsx";
import { ServerFilesTab, versionDate } from "./ServerFilesTab.tsx";

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
// Keep the real module (isAbortError, DownloadTooLargeError, the size cap)
// and stub only the two network entry points.
vi.mock("../api/download.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/download.ts")>()),
  ...mockDownload,
}));

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

function renderPage(entry = `/communities/${CID}/servers/${SID}`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = render(
    <MemoryRouter initialEntries={[entry]}>
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
  return { ...result, queryClient };
}

async function openFiles() {
  await screen.findByText("survival");
  fireEvent.click(
    screen.getByRole("tab", { name: t("serverDetail.tab.files") }),
  );
}

// Render ServerFilesTab on its own so a test can inject the bulk-download
// aggregate cap (#2063). ServerDetailPage never passes that prop, so the whole
// page can't reach it; the tab takes `server`/`communityId`/`can` directly.
function renderFilesTab(
  overrides: Partial<ComponentProps<typeof ServerFilesTab>> = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={[`/communities/${CID}/servers/${SID}`]}>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <ServerFilesTab
            server={
              server() as unknown as ComponentProps<
                typeof ServerFilesTab
              >["server"]
            }
            communityId={CID}
            can={() => true}
            {...overrides}
          />
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>,
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
      expect(mockApi.get).toHaveBeenCalledWith(
        `${FILES_BASE}?path=&list=true`,
        { signal: expect.any(AbortSignal) },
      ),
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
        { signal: expect.any(AbortSignal) },
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

  it("keeps a raw latin-1 byte in server.properties through an unrelated edit (#2851)", async () => {
    // 0xE9 is "é" in latin-1 and not valid UTF-8; a UTF-8-only editor showed
    // U+FFFD and saved EF BF BD in its place.
    routeGet({
      detail: server(),
      list: listing([{ name: "server.properties", is_dir: false }]),
      content: {
        path: "server.properties",
        content_base64: btoa("motd=Caf\xe9\nmax-players=20\n"),
      },
    });
    mockApi.put.mockResolvedValue(undefined);
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/server\.properties/));
    const editor = (await screen.findByLabelText(
      t("files.editorLabel"),
    )) as HTMLTextAreaElement;
    expect(editor.value).toBe("motd=Café\nmax-players=20\n");

    fireEvent.change(editor, {
      target: { value: "motd=Café\nmax-players=30\n" },
    });
    fireEvent.click(screen.getByRole("button", { name: t("files.save") }));

    await waitFor(() => expect(mockApi.put).toHaveBeenCalled());
    const body = JSON.parse(
      (mockApi.put.mock.calls[0][1] as { body: string }).body,
    );
    expect(body.content_base64).toBe(btoa("motd=Caf\xe9\nmax-players=30\n"));
  });

  it("refuses to save a character a latin-1 file cannot hold (#2851)", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "server.properties", is_dir: false }]),
      content: {
        path: "server.properties",
        content_base64: btoa("motd=Caf\xe9\n"),
      },
    });
    mockApi.put.mockResolvedValue(undefined);
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/server\.properties/));
    const editor = await screen.findByLabelText(t("files.editorLabel"));
    fireEvent.change(editor, { target: { value: "motd=Café 🐉\n" } });
    fireEvent.click(screen.getByRole("button", { name: t("files.save") }));

    expect(
      await screen.findByText(t("files.error.unencodableText")),
    ).toBeInTheDocument();
    expect(mockApi.put).not.toHaveBeenCalled();
  });

  // Before 1.20 Minecraft reads server.properties as latin-1 only, so the
  // editor reads and writes that one file as latin-1 whatever its bytes are.
  it("saves server.properties as latin-1 on a pre-1.20 server even when it was ASCII (#2851)", async () => {
    routeGet({
      detail: server({ mc_version: "1.19.4" }),
      list: listing([{ name: "server.properties", is_dir: false }]),
      content: {
        path: "server.properties",
        content_base64: btoa("motd=Cafe\n"),
      },
    });
    mockApi.put.mockResolvedValue(undefined);
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/server\.properties/));
    const editor = await screen.findByLabelText(t("files.editorLabel"));
    fireEvent.change(editor, { target: { value: "motd=Café\n" } });
    fireEvent.click(screen.getByRole("button", { name: t("files.save") }));

    await waitFor(() => expect(mockApi.put).toHaveBeenCalled());
    const body = JSON.parse(
      (mockApi.put.mock.calls[0][1] as { body: string }).body,
    );
    // E9, not the UTF-8 C3 A9 the server would read back as "Ã©".
    expect(body.content_base64).toBe(btoa("motd=Caf\xe9\n"));
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

  it("disables the Create button while the folder-create request is in flight", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "a.txt", is_dir: false }]),
    });
    // Keep the create request pending so the busy state stays observable.
    let resolveCreate: (() => void) | undefined;
    mockApi.post.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveCreate = resolve;
        }),
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
      target: { value: "datapacks" },
    });

    const create = screen.getByRole("button", { name: t("files.create") });
    fireEvent.click(create);

    // While the POST is pending the button is disabled, so a double-click
    // cannot fire a second (conflicting) request (#1591).
    await waitFor(() => expect(create).toBeDisabled());
    fireEvent.click(create);
    expect(mockApi.post).toHaveBeenCalledTimes(1);

    resolveCreate?.();
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
        expect.any(AbortSignal),
      ),
    );
  });

  // A directory download no longer goes through `downloadFile` at all — it is
  // saved from a minted grant URL (#2354). See the "Directory downloads"
  // describe below for the `.zip` naming this test used to pin (#2018).

  it("runs concurrent row downloads independently, aborting only on unmount (#1728)", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    // Emulate fetch's abort contract: stay pending until the signal fires.
    mockDownload.downloadFile.mockImplementation(
      (_path: string, _name: string, signal?: AbortSignal) =>
        new Promise<void>((_, reject) => {
          signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    );
    const { unmount } = renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    const startDownload = (name: RegExp) => {
      const row = screen.getByText(name).closest("li") as HTMLElement;
      fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
      fireEvent.click(
        screen.getByRole("menuitem", { name: t("files.contextMenu.download") }),
      );
    };

    startDownload(/a\.txt/);
    await waitFor(() =>
      expect(mockDownload.downloadFile).toHaveBeenCalledTimes(1),
    );
    startDownload(/b\.txt/);
    await waitFor(() =>
      expect(mockDownload.downloadFile).toHaveBeenCalledTimes(2),
    );

    // Concurrent row downloads are legitimate: neither cancels the other.
    const [first, second] = mockDownload.downloadFile.mock.calls.map(
      (c) => c[2] as AbortSignal,
    );
    expect(first.aborted).toBe(false);
    expect(second.aborted).toBe(false);

    unmount();

    // Leaving the view aborts everything still in flight.
    expect(first.aborted).toBe(true);
    expect(second.aborted).toBe(true);
  });

  it("viewer re-download supersedes the previous one without an error toast (#1728)", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return Promise.resolve({
          path: "notes.txt",
          content_base64: encodeUtf8Base64("hello\n"),
        });
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(listing([{ name: "notes.txt", is_dir: false }]));
      }
      return Promise.resolve(server());
    });
    // Emulate fetch's abort contract: stay pending until the signal fires.
    mockDownload.downloadFile.mockImplementation(
      (_path: string, _name: string, signal?: AbortSignal) =>
        new Promise<void>((_, reject) => {
          signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    );
    renderPage();
    await openFiles();
    fireEvent.click(await screen.findByText(/notes\.txt/));
    const downloadButton = await screen.findByRole("button", {
      name: t("files.download"),
    });

    fireEvent.click(downloadButton);
    await waitFor(() =>
      expect(mockDownload.downloadFile).toHaveBeenCalledTimes(1),
    );
    const firstSignal = mockDownload.downloadFile.mock
      .calls[0][2] as AbortSignal;

    fireEvent.click(downloadButton);
    await waitFor(() =>
      expect(mockDownload.downloadFile).toHaveBeenCalledTimes(2),
    );

    // Re-requesting the same file supersedes the stale attempt...
    expect(firstSignal.aborted).toBe(true);
    // ...and the intentional cancel must not surface as an error toast.
    expect(
      screen.queryByText(t("files.error.generic")),
    ).not.toBeInTheDocument();
  });

  it("aborts the file-content request when the tab unmounts mid-load (#1728)", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return new Promise(() => {}); // content GET stays in flight forever
      }
      if (path.includes("/files?path=")) {
        return Promise.resolve(listing([{ name: "notes.txt", is_dir: false }]));
      }
      return Promise.resolve(server());
    });
    const { unmount } = renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/notes\.txt/));
    await waitFor(() =>
      expect(
        mockApi.get.mock.calls.some((c) =>
          (c[0] as string).includes("?path=notes.txt"),
        ),
      ).toBe(true),
    );
    const contentCall = mockApi.get.mock.calls.find((c) =>
      (c[0] as string).includes("?path=notes.txt"),
    ) as [string, { signal: AbortSignal }];
    expect(contentCall[1].signal.aborted).toBe(false);

    unmount();

    expect(contentCall[1].signal.aborted).toBe(true);
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
        { signal: expect.any(AbortSignal) },
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
        { signal: expect.any(AbortSignal) },
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
        { signal: expect.any(AbortSignal) },
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

  it("previews a version's content read-only when its date is clicked", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("/files/version")) {
        return Promise.resolve({
          path: "a b.txt",
          content_base64: encodeUtf8Base64("OLD VERSION"),
        });
      }
      if (path.includes("/files/history")) {
        return Promise.resolve({ path: "a b.txt", versions: [VID1] });
      }
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return Promise.resolve({
          path: "a b.txt",
          content_base64: encodeUtf8Base64("current"),
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

    const date1 = versionDate(VID1).toLocaleString();
    fireEvent.click(await screen.findByText(date1));

    expect(
      await screen.findByText(t("files.history.preview.title")),
    ).toBeInTheDocument();
    // The version's bytes render read-only (not the current file's content).
    const previewArea = await screen.findByDisplayValue("OLD VERSION");
    expect(previewArea).toHaveAttribute("readonly");

    await waitFor(() =>
      expect(mockApi.get).toHaveBeenCalledWith(
        `${FILES_BASE}/version?path=a%20b.txt&version_id=${VID1}`,
        { signal: expect.any(AbortSignal) },
      ),
    );
  });

  it("shows a not-previewable message for a binary version", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("/files/version")) {
        return Promise.resolve({
          path: "a b.txt",
          // A leading NUL byte makes isProbablyText() classify it as binary.
          content_base64: encodeUtf8Base64("\u0000binary"),
        });
      }
      if (path.includes("/files/history")) {
        return Promise.resolve({ path: "a b.txt", versions: [VID1] });
      }
      if (path.includes("/files?path=") && !path.includes("list=")) {
        return Promise.resolve({
          path: "a b.txt",
          content_base64: encodeUtf8Base64("current\n"),
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

    fireEvent.click(
      await screen.findByText(versionDate(VID1).toLocaleString()),
    );

    expect(
      await screen.findByText(t("files.history.preview.binary")),
    ).toBeInTheDocument();
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
});

describe("ServerFilesTab error presentation glue", () => {
  it("names the platform-managed key a refused save would change (#2790)", async () => {
    // The 422 carries the offending key in a `key` extension member; without an
    // arm for the reason it collapsed into the generic invalid-input toast and
    // the user was never told which line to revert.
    routeGet({
      detail: server(),
      list: listing([{ name: "server.properties", is_dir: false }]),
      content: {
        path: "server.properties",
        content_base64: encodeUtf8Base64("motd=hello\n"),
      },
    });
    mockApi.put.mockRejectedValue(
      new ApiError(422, {
        reason: "platform_managed_key",
        key: "rcon.password",
      }),
    );
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/server\.properties/));
    const editor = await screen.findByLabelText(t("files.editorLabel"));
    fireEvent.change(editor, { target: { value: "rcon.password=hunter2\n" } });
    fireEvent.click(screen.getByRole("button", { name: t("files.save") }));

    expect(
      await screen.findByText(
        t("files.error.platformManagedKey", { key: "rcon.password" }),
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(t("files.error.invalidInput")),
    ).not.toBeInTheDocument();
  });
});

describe("ServerFilesTab protected content flow", () => {
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

  it("excludes oversized files from the progress denominator", async () => {
    routeGet({ detail: server(), list: listing([]) });
    // Simulate the upload completing by invoking the progress callback with
    // the full file size, then hold the promise so the progress bar stays
    // visible for inspection.
    let resolveUpload: (() => void) | undefined;
    mockPostFormWithProgress.mockImplementation(
      (_url: string, form: FormData, onProgress: (loaded: number) => void) => {
        const file = form.get("file") as File;
        onProgress(file.size);
        return new Promise<void>((resolve) => {
          resolveUpload = resolve;
        });
      },
    );
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const tree = document.querySelector(".file-tree") as HTMLElement;
    const small = new File(["hello"], "small.txt");
    const big = new File(["x"], "huge.bin");
    // Fake the size to exceed the 512 MiB cap.
    Object.defineProperty(big, "size", { value: 600 * 1024 * 1024 });

    fireEvent.drop(tree, { dataTransfer: dataTransfer([small, big]) });

    // The progress bar should reach 100% because the oversized file's bytes
    // must not inflate the denominator.
    await waitFor(() => {
      const bar = screen.getByRole("progressbar");
      expect(bar).toHaveAttribute("aria-valuenow", "100");
    });

    // Only the small file was uploaded.
    expect(mockPostFormWithProgress).toHaveBeenCalledTimes(1);

    // The too-large toast fired.
    expect(screen.getByText(t("files.error.tooLarge"))).toBeInTheDocument();

    // Clean up: resolve the pending upload promise.
    resolveUpload?.();
  });

  it("does not arm the progress bar when all dropped files are oversized", async () => {
    routeGet({ detail: server(), list: listing([]) });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const tree = document.querySelector(".file-tree") as HTMLElement;
    const big = new File(["x"], "huge.bin");
    Object.defineProperty(big, "size", { value: 600 * 1024 * 1024 });

    fireEvent.drop(tree, { dataTransfer: dataTransfer([big]) });

    // Advance past the first yield in onDrop (the preparing-indicator phase).
    // Do NOT wrap in act() — act flushes all pending async work, which would
    // also process the handler's second yield and run progress.reset(),
    // hiding the bug. A bare setTimeout lets the handler pause between
    // progress.start() (armed) and progress.reset() (disarmed), exposing
    // the progress bar if it was incorrectly armed.
    await new Promise((r) => setTimeout(r, 0));

    // With the pre-fix code, progress.start() fires during the tick above
    // and the handler yields again before the upload loop — the progress
    // bar would be in the DOM here. With the fix, the oversized file is
    // filtered out and the handler returns early, so no bar appears.
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();

    // No upload should have been attempted.
    expect(mockPostFormWithProgress).not.toHaveBeenCalled();

    // The too-large toast should have fired.
    await waitFor(() =>
      expect(screen.getByText(t("files.error.tooLarge"))).toBeInTheDocument(),
    );
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
      expect.any(AbortSignal),
    );
    expect(mockDownload.fetchFileBlob).toHaveBeenCalledWith(
      `${FILES_BASE}/download?path=b.txt`,
      expect.any(AbortSignal),
    );
    // downloadFile should NOT have been called (ZIP handles both files).
    expect(mockDownload.downloadFile).not.toHaveBeenCalled();
  });

  it("aborts the bulk ZIP once the running byte total passes the aggregate cap (#2063)", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "world", is_dir: true },
        { name: "world_nether", is_dir: true },
        { name: "world_the_end", is_dir: true },
      ]),
    });
    // Each directory response is individually under the per-fetch cap (#2027),
    // and the pre-check prices directories at 0 — so only the running total in
    // the loop can catch the sum. Feed a fixed 8-byte blob per fetch and inject
    // a 10-byte cap: the total crosses on the second fetch (16 > 10) without
    // allocating anything near 512 MiB.
    mockDownload.fetchFileBlob.mockResolvedValue(new Blob(["12345678"]));
    const createObjectURL = vi.spyOn(URL, "createObjectURL");
    renderFilesTab({ maxBulkDownloadBytes: 10 });
    await screen.findByText(/world_nether/);

    fireEvent.click(screen.getByRole("checkbox", { name: "world" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "world_nether" }), {
      ctrlKey: true,
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "world_the_end" }), {
      ctrlKey: true,
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("files.bulk.download") }),
    );

    // The guard surfaces the size-bearing too-large toast and stops.
    await screen.findByText(
      t("files.bulk.download.tooLarge", { size: humanizeBytes(16) }),
    );
    // It aborts before the third fetch and never builds/saves a ZIP.
    expect(mockDownload.fetchFileBlob).toHaveBeenCalledTimes(2);
    expect(createObjectURL).not.toHaveBeenCalled();
    createObjectURL.mockRestore();
  });

  it("aborts the bulk ZIP download when the tab unmounts mid-fetch (#1728)", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "a.txt", is_dir: false },
        { name: "b.txt", is_dir: false },
      ]),
    });
    // Emulate fetch's abort contract: stay pending until the signal fires.
    mockDownload.fetchFileBlob.mockImplementation(
      (_path: string, signal?: AbortSignal) =>
        new Promise<Blob>((_, reject) => {
          signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    );
    const { unmount } = renderPage();
    await openFiles();
    await screen.findByText(/a\.txt/);

    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "b.txt" }), {
      ctrlKey: true,
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("files.bulk.download") }),
    );

    // The loop is sequential: the first file's fetch is in flight.
    await waitFor(() =>
      expect(mockDownload.fetchFileBlob).toHaveBeenCalledTimes(1),
    );
    const signal = mockDownload.fetchFileBlob.mock.calls[0][1] as AbortSignal;
    expect(signal.aborted).toBe(false);

    unmount();

    expect(signal.aborted).toBe(true);
    // The aborted loop stops instead of fetching the remaining files.
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockDownload.fetchFileBlob).toHaveBeenCalledTimes(1);
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
      expect.any(AbortSignal),
    );
    expect(mockDownload.fetchFileBlob).not.toHaveBeenCalled();
  });

  // A single selected directory no longer goes through `downloadFile` either
  // (#2354) — see the "Directory downloads" describe below.

  it("names a bulk-selected directory's nested ZIP entry with .zip (#2018)", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "world", is_dir: true },
        { name: "a.txt", is_dir: false },
      ]),
    });
    const blobs: Blob[] = [];
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockImplementation((obj: Blob | MediaSource) => {
        blobs.push(obj as Blob);
        return "blob:fake";
      });
    renderPage();
    await openFiles();
    await screen.findByText(/world/);

    fireEvent.click(screen.getByRole("checkbox", { name: "world" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }), {
      ctrlKey: true,
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("files.bulk.download") }),
    );

    await waitFor(() => expect(createObjectURL).toHaveBeenCalled());
    const names = Object.keys(
      unzipSync(new Uint8Array(await blobs[0].arrayBuffer())),
    );
    // The directory's bytes are themselves a ZIP: name the entry accordingly.
    expect(names).toContain("world.zip");
    expect(names).toContain("a.txt");
    createObjectURL.mockRestore();
  });

  it("keeps a directory and its sibling .zip file as distinct ZIP entries (#2018)", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "world", is_dir: true },
        { name: "world.zip", is_dir: false, size: 10 },
      ]),
    });
    // Distinct bytes per path so an overwrite is visible, not just a key clash.
    mockDownload.fetchFileBlob.mockImplementation((path: string) =>
      Promise.resolve(new Blob([path.endsWith("world") ? "DIR" : "FILE"])),
    );
    const blobs: Blob[] = [];
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockImplementation((obj: Blob | MediaSource) => {
        blobs.push(obj as Blob);
        return "blob:fake";
      });
    renderPage();
    await openFiles();
    await screen.findByRole("checkbox", { name: "world.zip" });

    // Select the directory first: suffixing `world` -> `world.zip` would claim
    // the sibling file's own name and silently drop one of the two.
    fireEvent.click(screen.getByRole("checkbox", { name: "world" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "world.zip" }), {
      ctrlKey: true,
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("files.bulk.download") }),
    );

    await waitFor(() => expect(createObjectURL).toHaveBeenCalled());
    const archive = unzipSync(new Uint8Array(await blobs[0].arrayBuffer()));
    // Both selected items survive, and neither overwrote the other.
    expect(Object.keys(archive)).toHaveLength(2);
    const decode = (name: string) => new TextDecoder().decode(archive[name]);
    expect(decode("world.zip")).toBe("FILE");
    expect(decode("world")).toBe("DIR");
    createObjectURL.mockRestore();
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

// ── Keyboard shortcuts (issue #1465) ──────────────────────────────────────────

describe("Keyboard shortcuts", () => {
  it.each([
    ["Ctrl+A", { ctrlKey: true }],
    ["Cmd+A", { metaKey: true }],
  ])("%s selects all items", async (_shortcut, modifier) => {
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

    fireEvent.keyDown(document, { key: "a", ...modifier });

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

  it.each(["Delete", "Backspace"])(
    "%s opens and cancels delete confirmation for a selected item",
    async (key) => {
      routeGet({
        detail: server(),
        list: listing([{ name: "a.txt", is_dir: false }]),
      });
      renderPage();
      await openFiles();
      await screen.findByText(/a\.txt/);

      fireEvent.click(screen.getByRole("checkbox", { name: "a.txt" }));
      fireEvent.keyDown(document, { key });

      await screen.findByText(t("files.delete.dialogTitle"));
      fireEvent.click(screen.getByRole("button", { name: t("common.cancel") }));

      await waitFor(() =>
        expect(
          screen.queryByText(t("files.delete.dialogTitle")),
        ).not.toBeInTheDocument(),
      );
    },
  );

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

  it("F2 opens and cancels rename dialog for a single selected item", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/readme\.txt/);

    fireEvent.click(screen.getByRole("checkbox", { name: "readme.txt" }));

    fireEvent.keyDown(document, { key: "F2" });

    await screen.findByText(t("files.newName"));
    fireEvent.click(screen.getByRole("button", { name: t("common.cancel") }));

    await waitFor(() =>
      expect(screen.queryByText(t("files.newName"))).not.toBeInTheDocument(),
    );
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
        { signal: expect.any(AbortSignal) },
      ),
    );
  });
});

// ── Unsaved changes guard (issue #1486) ────────────────────────────────────

describe("ServerFilesTab unsaved changes guard", () => {
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
        { signal: expect.any(AbortSignal) },
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
});

// ── Overwrite confirmation dialog ─────────────────────────────────────────────

describe("ServerFilesTab overwrite confirmation", () => {
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

  describe("drag-and-drop overwrite", () => {
    function dataTransfer(files: File[]): DataTransfer {
      return {
        files,
        types: files.length > 0 ? ["Files"] : [],
      } as unknown as DataTransfer;
    }

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

// ── Copy/paste (issue #1465) ──────────────────────────────────────────────────

describe("Copy/paste", () => {
  it("Ctrl+C copies selected files", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/readme\.txt/);

    // Select the file.
    fireEvent.click(screen.getByRole("checkbox", { name: "readme.txt" }));

    // Press Ctrl+C.
    fireEvent.keyDown(document, { key: "c", ctrlKey: true });

    // Should see copied toast.
    expect(await screen.findByText(t("files.copied"))).toBeInTheDocument();
  });

  it("Ctrl+V pastes copied files (download + upload)", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/readme\.txt/);

    // Select and copy.
    fireEvent.click(screen.getByRole("checkbox", { name: "readme.txt" }));
    fireEvent.keyDown(document, { key: "c", ctrlKey: true });
    await screen.findByText(t("files.copied"));

    // Paste.
    fireEvent.keyDown(document, { key: "v", ctrlKey: true });

    // Same-name file exists, so overwrite dialog appears first.
    await screen.findByText(t("files.overwrite.title"));
    fireEvent.click(
      screen.getByRole("button", { name: t("files.overwrite.overwrite") }),
    );

    // Should download and re-upload the file.
    await waitFor(() =>
      expect(mockDownload.fetchFileBlob).toHaveBeenCalledWith(
        `${FILES_BASE}/download?path=readme.txt`,
      ),
    );
    await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
    const [url] = mockPostFormWithProgress.mock.calls[0];
    expect(url).toBe(`${FILES_BASE}/upload?path=&extract=false`);
  });

  it("paste via context menu triggers download + upload", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/readme\.txt/);

    // Copy via context menu.
    const row = screen.getByText(/readme\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.copy") }),
    );
    await screen.findByText(t("files.copied"));

    // Paste via context menu on empty space.
    const fileList = document.querySelector(".file-list") as HTMLElement;
    fireEvent.contextMenu(fileList, { clientX: 50, clientY: 50 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.paste") }),
    );

    // Same-name file exists, so overwrite dialog appears.
    await screen.findByText(t("files.overwrite.title"));
    fireEvent.click(
      screen.getByRole("button", { name: t("files.overwrite.overwrite") }),
    );

    await waitFor(() =>
      expect(mockDownload.fetchFileBlob).toHaveBeenCalledWith(
        `${FILES_BASE}/download?path=readme.txt`,
      ),
    );
    await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
  });

  it("paste skips file when user clicks skip in overwrite dialog", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/readme\.txt/);

    // Copy and paste.
    fireEvent.click(screen.getByRole("checkbox", { name: "readme.txt" }));
    fireEvent.keyDown(document, { key: "c", ctrlKey: true });
    await screen.findByText(t("files.copied"));

    fireEvent.keyDown(document, { key: "v", ctrlKey: true });
    await screen.findByText(t("files.overwrite.title"));
    fireEvent.click(
      screen.getByRole("button", { name: t("files.overwrite.skip") }),
    );

    // No download or upload should occur.
    await waitFor(() =>
      expect(
        screen.queryByText(t("files.overwrite.title")),
      ).not.toBeInTheDocument(),
    );
    expect(mockDownload.fetchFileBlob).not.toHaveBeenCalled();
  });

  it("Ctrl+C filters out folders from selection", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "world", is_dir: true },
        { name: "readme.txt", is_dir: false },
      ]),
    });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/readme\.txt/);

    // Select all (includes the folder).
    fireEvent.keyDown(document, { key: "a", ctrlKey: true });
    await waitFor(() => {
      expect(screen.getByRole("checkbox", { name: "world" })).toBeChecked();
      expect(
        screen.getByRole("checkbox", { name: "readme.txt" }),
      ).toBeChecked();
    });

    // Copy — should only copy the file, not the folder.
    fireEvent.keyDown(document, { key: "c", ctrlKey: true });
    await screen.findByText(t("files.copied"));

    // Paste — should download only readme.txt.
    fireEvent.keyDown(document, { key: "v", ctrlKey: true });

    // Overwrite since readme.txt exists.
    await screen.findByText(t("files.overwrite.title"));
    fireEvent.click(
      screen.getByRole("button", { name: t("files.overwrite.overwrite") }),
    );

    await waitFor(() =>
      expect(mockDownload.fetchFileBlob).toHaveBeenCalledWith(
        `${FILES_BASE}/download?path=readme.txt`,
      ),
    );
    // Should have only downloaded one file (the file, not the folder).
    expect(mockDownload.fetchFileBlob).toHaveBeenCalledTimes(1);
  });
});

// ── Move to... context menu (issue #1465) ───────────────────────────────────

describe("Move to... context menu", () => {
  it("opens a dialog and moves the file to the destination", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "readme.txt", is_dir: false }]),
    });
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await openFiles();

    const row = (await screen.findByText(/readme\.txt/)).closest(
      "li",
    ) as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.moveTo") }),
    );

    // Move dialog appears with destination input.
    const input = screen.getByLabelText(t("files.bulk.move.destLabel"));
    fireEvent.change(input, { target: { value: "archive" } });
    fireEvent.click(
      screen.getByRole("button", { name: t("files.bulk.move.confirm") }),
    );

    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    const [url, init] = mockApi.post.mock.calls[0];
    expect(url).toBe(`${FILES_BASE}/rename`);
    expect(JSON.parse((init as { body: string }).body)).toEqual({
      from: "readme.txt",
      to: "archive/readme.txt",
    });
    expect(
      screen.queryByLabelText(t("files.bulk.move.destLabel")),
    ).not.toBeInTheDocument();
  });
});

// ── Arrow key navigation (issue #1465) ──────────────────────────────────────

describe("Arrow key navigation", () => {
  it("moves between rows and clamps focus at each boundary", async () => {
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

    const buttons = document.querySelectorAll(".file-list .file-name");
    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(document.activeElement).toBe(buttons[0]);

    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(document.activeElement).toBe(buttons[1]);

    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(document.activeElement).toBe(buttons[1]);

    fireEvent.keyDown(document, { key: "ArrowUp" });
    expect(document.activeElement).toBe(buttons[0]);

    fireEvent.keyDown(document, { key: "ArrowUp" });
    expect(document.activeElement).toBe(buttons[0]);

    (buttons[0] as HTMLElement).blur();
    fireEvent.keyDown(document, { key: "ArrowUp" });
    expect(document.activeElement).toBe(buttons[1]);
  });
});

describe("ServerFilesTab refetch failure (#1805)", () => {
  let restoreWs: () => void;
  beforeEach(() => {
    restoreWs = installMockWebSocket();
    setAccessToken("tok-1");
    mockApi.get.mockReset();
    mockApi.put.mockReset();
    mockApi.post.mockReset();
    mockApi.delete.mockReset();
    mockCan = () => true;
  });
  afterEach(() => {
    restoreWs();
    vi.clearAllMocks();
  });

  it("keeps rendering cached file listing when a background refetch fails", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "server.properties", is_dir: false }]),
    });
    const { queryClient } = renderPage();
    await openFiles();
    await screen.findByText(/server\.properties/);

    // Simulate a transient API outage: the next background refetch fails.
    mockApi.get.mockRejectedValue(new ApiError(500, {}));
    await act(() => queryClient.invalidateQueries());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // The cached listing stays on screen instead of the error.
    expect(screen.getByText(/server\.properties/)).toBeInTheDocument();
    expect(screen.queryByText(t("files.listError"))).not.toBeInTheDocument();
  });
});

// ── Directory downloads via a minted grant (#2354) ────────────────────────────

describe("ServerFilesTab directory downloads (minted grant, #2354)", () => {
  const GRANT_PATH = `${FILES_BASE}/download-grant?path=world`;
  const GRANT_URL = `${FILES_BASE}/download?path=world&grant=jwt-1`;
  const clicks: HTMLAnchorElement[] = [];
  let fetchSpy: MockInstance<typeof fetch>;

  /** URLs the tab requested itself, rather than handing to the browser. */
  const fetchedUrls = () => fetchSpy.mock.calls.map(([input]) => String(input));

  beforeEach(() => {
    clicks.length = 0;
    // Capture the synthesised anchor click instead of navigating.
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicks.push(this);
    });
    fetchSpy = vi.spyOn(globalThis, "fetch");
    mockApi.post.mockResolvedValue({
      download_url: GRANT_URL,
      expires_at: "2026-07-27T04:00:30Z",
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function downloadDirFromContextMenu() {
    const row = screen.getByText(/world/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", {
        name: t("files.contextMenu.downloadZip"),
      }),
    );
  }

  it("mints a grant for a row download and saves it as {name}.zip", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "world", is_dir: true }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/world/);

    downloadDirFromContextMenu();

    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith(GRANT_PATH));
    await waitFor(() => expect(clicks).toHaveLength(1));
    expect(clicks[0].getAttribute("href")).toBe(GRANT_URL);
    // The API streams the directory as a ZIP, so the saved file must be named
    // `world.zip` — not the bare entry name (#2018).
    expect(clicks[0].download).toBe("world.zip");
    // The zip is never buffered into the tab: no capped fetch+Blob path, and no
    // request to the grant URL at all — only the browser fetches it.
    expect(mockDownload.downloadFile).not.toHaveBeenCalled();
    expect(fetchedUrls().filter((url) => url.includes("grant="))).toEqual([]);
  });

  it("mints a grant for a single selected directory in a bulk download", async () => {
    routeGet({
      detail: server(),
      list: listing([
        { name: "world", is_dir: true },
        { name: "a.txt", is_dir: false },
      ]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/world/);

    fireEvent.click(screen.getByRole("checkbox", { name: "world" }));
    fireEvent.click(
      screen.getByRole("button", { name: t("files.bulk.download") }),
    );

    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith(GRANT_PATH));
    await waitFor(() => expect(clicks).toHaveLength(1));
    expect(clicks[0].getAttribute("href")).toBe(GRANT_URL);
    expect(clicks[0].download).toBe("world.zip");
    expect(mockDownload.downloadFile).not.toHaveBeenCalled();
    expect(mockDownload.fetchFileBlob).not.toHaveBeenCalled();
  });

  it("keeps a row file download on the capped fetch, minting nothing", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "log.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/log\.txt/);

    const row = screen.getByText(/log\.txt/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.download") }),
    );

    await waitFor(() =>
      expect(mockDownload.downloadFile).toHaveBeenCalledWith(
        `${FILES_BASE}/download?path=log.txt`,
        "log.txt",
        expect.any(AbortSignal),
      ),
    );
    expect(mockApi.post).not.toHaveBeenCalled();
    expect(clicks).toHaveLength(0);
  });

  it("still rejects an oversize file up front with the too-large toast", async () => {
    const size = 600 * 1024 * 1024;
    routeGet({
      detail: server(),
      list: listing([{ name: "big.bin", is_dir: false, size }]),
    });
    mockDownload.downloadFile.mockRejectedValue(
      new DownloadTooLargeError(size),
    );
    renderPage();
    await openFiles();
    await screen.findByText(/big\.bin/);

    const row = screen.getByText(/big\.bin/).closest("li") as HTMLElement;
    fireEvent.contextMenu(row, { clientX: 100, clientY: 200 });
    fireEvent.click(
      screen.getByRole("menuitem", { name: t("files.contextMenu.download") }),
    );

    expect(
      await screen.findByText(
        t("files.error.downloadTooLarge", { size: humanizeBytes(size) }),
      ),
    ).toBeInTheDocument();
    expect(clicks).toHaveLength(0);
  });

  it("shows the must-be-stopped toast when the mint 409s, and saves nothing", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "world", is_dir: true }]),
    });
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "server_unsettled" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(/world/);

    downloadDirFromContextMenu();

    expect(
      await screen.findByText(t("files.error.serverMustBeStopped")),
    ).toBeInTheDocument();
    expect(clicks).toHaveLength(0);
  });

  it("routes a mint 403 through the permission glue, and saves nothing", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "world", is_dir: true }]),
    });
    mockApi.post.mockRejectedValue(
      new ApiError(403, { reason: "forbidden", permission: "file:read" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(/world/);

    downloadDirFromContextMenu();

    expect(
      await screen.findByText(
        t("permissions.deniedNamed", { permission: "file:read" }),
      ),
    ).toBeInTheDocument();
    expect(clicks).toHaveLength(0);
  });
});
