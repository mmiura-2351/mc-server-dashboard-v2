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
    execution_backend: "container",
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

  it("offers download only for a binary file (no editor)", async () => {
    const binary = btoa(String.fromCharCode(0x50, 0x4b, 0x03, 0x04, 0x00));
    routeGet({
      detail: server(),
      list: listing([{ name: "region.mca", is_dir: false }]),
      content: { path: "region.mca", content_base64: binary },
    });
    renderPage();
    await openFiles();

    fireEvent.click(await screen.findByText(/region\.mca/));
    expect(await screen.findByText(t("files.binary"))).toBeInTheDocument();
    expect(
      screen.queryByLabelText(t("files.editorLabel")),
    ).not.toBeInTheDocument();
  });
});

describe("ServerFilesTab operations", () => {
  it("uploads via multipart with ?extract= reflecting the toggle", async () => {
    routeGet({
      detail: server(),
      list: listing([]),
    });
    mockPostFormWithProgress.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    fireEvent.click(screen.getByLabelText(t("files.extractZip")));
    const file = new File(["x"], "world.zip");
    fireEvent.change(screen.getByLabelText(t("files.upload")), {
      target: { files: [file] },
    });

    await waitFor(() => expect(mockPostFormWithProgress).toHaveBeenCalled());
    const [url, form] = mockPostFormWithProgress.mock.calls[0];
    expect(url).toBe(`${FILES_BASE}/upload?path=&extract=true`);
    expect((form as FormData).get("file")).toBe(file);
  });

  it("creates a directory at the current path", async () => {
    routeGet({ detail: server(), list: listing([]) });
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    fireEvent.click(screen.getByRole("button", { name: t("files.newFolder") }));
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

  it("renames an entry with a {from, to} body", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "old.txt", is_dir: false }]),
    });
    mockApi.post.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/old\.txt/);

    fireEvent.click(screen.getByRole("button", { name: t("files.rename") }));
    const input = screen.getByLabelText(t("files.newName"));
    fireEvent.change(input, { target: { value: "new.txt" } });
    // The dialog confirm button shares the "Rename" label; pick the modal one.
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

  it("deletes after a typed confirm with ?path=", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "junk.txt", is_dir: false }]),
    });
    mockApi.delete.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(/junk\.txt/);

    fireEvent.click(screen.getByRole("button", { name: t("files.delete") }));
    fireEvent.click(
      screen.getByRole("button", { name: t("files.delete.confirm") }),
    );

    await waitFor(() =>
      expect(mockApi.delete).toHaveBeenCalledWith(
        `${FILES_BASE}?path=junk.txt`,
      ),
    );
  });

  it("downloads a file via the authenticated helper", async () => {
    routeGet({
      detail: server(),
      list: listing([{ name: "log.txt", is_dir: false }]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(/log\.txt/);

    fireEvent.click(screen.getByRole("button", { name: t("files.download") }));
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

    expect(
      screen.queryByRole("button", { name: t("files.newFolder") }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("files.delete") }),
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

    fireEvent.click(screen.getByRole("button", { name: t("files.delete") }));
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

describe("ServerFilesTab history + rollback", () => {
  it("lists retained versions from files/history with an encoded path", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("/files/history")) {
        return Promise.resolve({ path: "a b.txt", versions: ["v1", "v2"] });
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

    expect(await screen.findByText("v1")).toBeInTheDocument();
    expect(screen.getByText("v2")).toBeInTheDocument();
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
        return Promise.resolve({ path: "a b.txt", versions: ["v1"] });
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
    await screen.findByText("v1");

    fireEvent.click(
      screen.getByRole("button", { name: t("files.history.rollback") }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("files.rollback.confirm") }),
    );

    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    const [url, init] = mockApi.post.mock.calls[0];
    expect(url).toBe(`${FILES_BASE}/rollback?path=a%20b.txt`);
    expect(JSON.parse((init as { body: string }).body)).toEqual({
      version_id: "v1",
    });
  });

  it("Escape closes only the rollback confirm, leaving the history drawer open", async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes("/files/history")) {
        return Promise.resolve({ path: "a b.txt", versions: ["v1"] });
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
    await screen.findByText("v1");

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
        return Promise.resolve({ path: "a.txt", versions: ["v1"] });
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
    await screen.findByText("v1");

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

  it("disables Upload and New folder while the server is running", async () => {
    routeGet({
      detail: server({ observed_state: "running", desired_state: "running" }),
      list: listing([]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.runningNotice"));

    const uploadBtn = screen.getByRole("button", { name: t("files.upload") });
    const newFolderBtn = screen.getByRole("button", {
      name: t("files.newFolder"),
    });
    expect(uploadBtn).toBeDisabled();
    expect(newFolderBtn).toBeDisabled();
  });

  it("enables Upload and New folder while the server is stopped", async () => {
    routeGet({
      detail: server({ observed_state: "stopped" }),
      list: listing([]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const uploadBtn = screen.getByLabelText(t("files.upload"));
    const newFolderBtn = screen.getByRole("button", {
      name: t("files.newFolder"),
    });
    expect(uploadBtn).not.toBeDisabled();
    expect(newFolderBtn).not.toBeDisabled();
  });

  it("disables Upload and New folder while the server is stopping (transitional)", async () => {
    routeGet({
      detail: server({ observed_state: "stopping", desired_state: "stopped" }),
      list: listing([]),
    });
    renderPage();
    await openFiles();
    await screen.findByText(t("files.runningNotice"));

    const uploadBtn = screen.getByRole("button", { name: t("files.upload") });
    const newFolderBtn = screen.getByRole("button", {
      name: t("files.newFolder"),
    });
    expect(uploadBtn).toBeDisabled();
    expect(newFolderBtn).toBeDisabled();
  });
});

describe("ServerFilesTab 409 reason toasts", () => {
  it("maps server_unsettled to the stop-the-server message on upload", async () => {
    // Use a stopped server so the file input is rendered; the API then returns
    // a 409 to exercise the error handler (e.g. a race: server started between
    // the UI check and the API call).
    routeGet({
      detail: server({ observed_state: "stopped" }),
      list: listing([]),
    });
    mockPostFormWithProgress.mockRejectedValue(
      new ApiError(409, { reason: "server_unsettled" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const fileInput = screen.getByLabelText(t("files.upload"));
    const file = new File(["x"], "test.zip");
    fireEvent.change(fileInput, { target: { files: [file] } });

    expect(
      await screen.findByText(t("files.error.serverMustBeStopped")),
    ).toBeInTheDocument();
  });

  it("maps server_not_stopped to the stop-the-server message on mkdir", async () => {
    routeGet({ detail: server(), list: listing([]) });
    mockApi.post.mockRejectedValue(
      new ApiError(409, { reason: "server_not_stopped" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    fireEvent.click(screen.getByRole("button", { name: t("files.newFolder") }));
    fireEvent.change(screen.getByLabelText(t("files.folderName")), {
      target: { value: "mods" },
    });
    fireEvent.click(screen.getByRole("button", { name: t("files.create") }));

    expect(
      await screen.findByText(t("files.error.serverMustBeStopped")),
    ).toBeInTheDocument();
  });

  it("falls back to the generic message for other errors", async () => {
    routeGet({ detail: server(), list: listing([]) });
    mockApi.post.mockRejectedValue(new ApiError(500, undefined));
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    fireEvent.click(screen.getByRole("button", { name: t("files.newFolder") }));
    fireEvent.change(screen.getByLabelText(t("files.folderName")), {
      target: { value: "mods" },
    });
    fireEvent.click(screen.getByRole("button", { name: t("files.create") }));

    expect(
      await screen.findByText(t("files.error.generic")),
    ).toBeInTheDocument();
  });

  it("shows a redirect notice with a link to #plugins on content_dir_protected (paper)", async () => {
    routeGet({
      detail: server({ server_type: "paper" }),
      list: listing([]),
    });
    mockPostFormWithProgress.mockRejectedValue(
      new ApiError(409, { reason: "content_dir_protected" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const fileInput = screen.getByLabelText(t("files.upload"));
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
      list: listing([]),
    });
    mockPostFormWithProgress.mockRejectedValue(
      new ApiError(409, { reason: "content_dir_protected" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    const fileInput = screen.getByLabelText(t("files.upload"));
    const file = new File(["x"], "test.jar");
    fireEvent.change(fileInput, { target: { files: [file] } });

    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent(t("serverDetail.tab.mods"));
  });
});
