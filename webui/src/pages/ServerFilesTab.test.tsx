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
  postForm: vi.fn(),
  put: vi.fn(),
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

vi.mock("react-router", async () => {
  const actual =
    await vi.importActual<typeof import("react-router")>("react-router");
  return { ...actual, useNavigate: () => vi.fn() };
});

const FILES_BASE = `/communities/${CID}/servers/${SID}/files`;

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
  mockApi.postForm.mockReset();
  mockApi.put.mockReset();
  mockApi.patch.mockReset();
  mockApi.delete.mockReset();
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
    mockApi.postForm.mockResolvedValue(undefined);
    renderPage();
    await openFiles();
    await screen.findByText(t("files.empty"));

    fireEvent.click(screen.getByLabelText(t("files.extractZip")));
    const file = new File(["x"], "world.zip");
    fireEvent.change(screen.getByLabelText(t("files.upload")), {
      target: { files: [file] },
    });

    await waitFor(() => expect(mockApi.postForm).toHaveBeenCalled());
    const [url, form] = mockApi.postForm.mock.calls[0];
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
    // ConfirmDialog gates on typing the exact name.
    fireEvent.change(screen.getByPlaceholderText("junk.txt"), {
      target: { value: "junk.txt" },
    });
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
      new ApiError(403, { reason: "file:edit" }),
    );
    renderPage();
    await openFiles();
    await screen.findByText("📄 x");

    fireEvent.click(screen.getByRole("button", { name: t("files.delete") }));
    fireEvent.change(screen.getByPlaceholderText("x"), {
      target: { value: "x" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: t("files.delete.confirm") }),
    );

    expect(
      await screen.findByText(`${t("permissions.deniedNamed")}file:edit`),
    ).toBeInTheDocument();
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
});
