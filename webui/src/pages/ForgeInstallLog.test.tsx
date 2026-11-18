import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
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
import { t } from "../i18n/index.ts";
import {
  ForgeInstallLog,
  INSTALL_LOG_TAIL_LINES,
  tailLines,
} from "./ForgeInstallLog.tsx";

const mockApi = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock("../api/client.ts", async () => {
  const actual =
    await vi.importActual<typeof import("../api/client.ts")>(
      "../api/client.ts",
    );
  return { ...actual, api: mockApi };
});

function fileBody(text: string) {
  return { path: "logs/forge-install.log", content_base64: btoa(text) };
}

function renderLog() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ForgeInstallLog communityId="c1" serverId="s1" />
    </QueryClientProvider>,
  );
  return client;
}

function open() {
  fireEvent.click(
    screen.getByRole("button", { name: t("serverDetail.installLog.view") }),
  );
}

beforeEach(() => {
  mockApi.get.mockReset();
});

describe("tailLines", () => {
  it("returns a short text whole", () => {
    expect(tailLines("a\nb\n", 3)).toEqual({ text: "a\nb", truncated: false });
  });

  it("keeps only the last lines of a long text", () => {
    expect(tailLines("a\nb\nc\nd\n", 2)).toEqual({
      text: "c\nd",
      truncated: true,
    });
  });
});

describe("ForgeInstallLog", () => {
  it("does not read the log until it is opened", () => {
    renderLog();
    expect(mockApi.get).not.toHaveBeenCalled();
  });

  it("reads logs/forge-install.log through the files API and shows it", async () => {
    mockApi.get.mockResolvedValue(
      fileBody(
        "Downloading libraries\nThere was an error during installation\n",
      ),
    );
    renderLog();
    open();

    expect(
      await screen.findByText(/There was an error during installation/),
    ).toBeInTheDocument();
    expect(mockApi.get.mock.calls[0][0]).toBe(
      "/api/communities/c1/servers/s1/files?path=logs%2Fforge-install.log",
    );
    expect(
      screen.queryByText(
        t("serverDetail.installLog.truncated", {
          count: INSTALL_LOG_TAIL_LINES,
        }),
      ),
    ).not.toBeInTheDocument();
  });

  it("shows only the tail of a long log and says so", async () => {
    const lines = Array.from(
      { length: INSTALL_LOG_TAIL_LINES + 50 },
      (_, i) => `line-${i}`,
    );
    mockApi.get.mockResolvedValue(fileBody(`${lines.join("\n")}\n`));
    renderLog();
    open();

    const view = await screen.findByRole("log");
    expect(view.textContent).toContain(`line-${lines.length - 1}`);
    expect(view.textContent).toContain("line-50\n");
    expect(view.textContent).not.toContain("line-49\n");
    expect(
      screen.getByText(
        t("serverDetail.installLog.truncated", {
          count: INSTALL_LOG_TAIL_LINES,
        }),
      ),
    ).toBeInTheDocument();
  });

  it("says the log is missing on a 404", async () => {
    mockApi.get.mockRejectedValue(new ApiError(404, {}));
    renderLog();
    open();

    expect(
      await screen.findByText(t("serverDetail.installLog.missing")),
    ).toBeInTheDocument();
  });

  it("says to stop the server when the files API refuses an unsettled server", async () => {
    mockApi.get.mockRejectedValue(
      new ApiError(409, { reason: "server_unsettled" }),
    );
    renderLog();
    open();

    expect(
      await screen.findByText(t("serverDetail.installLog.unsettled")),
    ).toBeInTheDocument();
  });

  it("points at the Files tab when the log is too large to read inline", async () => {
    mockApi.get.mockRejectedValue(new ApiError(413, {}));
    renderLog();
    open();

    expect(
      await screen.findByText(t("serverDetail.installLog.tooLarge")),
    ).toBeInTheDocument();
  });

  it("reports any other failure without breaking the page", async () => {
    mockApi.get.mockRejectedValue(new ApiError(503, {}));
    renderLog();
    open();

    expect(
      await screen.findByText(t("serverDetail.installLog.error")),
    ).toBeInTheDocument();
  });

  // A refetch (window focus, an invalidation) that fails keeps the log the
  // operator is reading, scrolled where they left it, and says it is the
  // copy loaded earlier (#3818).
  it("keeps the last good log in place with an error banner when a refetch fails", async () => {
    mockApi.get.mockResolvedValueOnce(fileBody("Downloading\nboom\n"));
    const client = renderLog();
    open();
    const view = await screen.findByRole("log");
    view.scrollTop = 7;

    mockApi.get.mockRejectedValueOnce(new ApiError(503, {}));
    await act(() => client.refetchQueries());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      `${t("serverDetail.installLog.error")} ${t("serverDetail.installLog.stale")}`,
    );
    expect(screen.getByRole("log")).toBe(view);
    expect(view.textContent).toContain("boom");
    expect(view.scrollTop).toBe(7);

    // The next good read clears the banner; the view is still the same one.
    mockApi.get.mockResolvedValueOnce(fileBody("Downloading\nboom\n"));
    await act(() => client.refetchQueries());

    await waitFor(() =>
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("log")).toBe(view);
    expect(view.scrollTop).toBe(7);
  });

  it("closes again", async () => {
    mockApi.get.mockResolvedValue(fileBody("boom\n"));
    renderLog();
    open();
    await screen.findByText(/boom/);

    fireEvent.click(
      screen.getByRole("button", { name: t("serverDetail.installLog.hide") }),
    );
    expect(screen.queryByText(/boom/)).not.toBeInTheDocument();
  });
});

describe("ForgeInstallLog scroll position (#3285)", () => {
  // Nothing is laid out here, so give every element a scroll height of 18px
  // (`.log-view`'s 12px at line-height 1.5) per line of its text.
  const LINE_PX = 18;
  let scrollHeight: MockInstance;

  beforeEach(() => {
    scrollHeight = vi
      .spyOn(Element.prototype, "scrollHeight", "get")
      .mockImplementation(function (this: Element) {
        return (this.textContent ?? "").split("\n").length * LINE_PX;
      });
  });

  afterEach(() => {
    scrollHeight.mockRestore();
  });

  function logLines(count: number) {
    return fileBody(
      `${Array.from({ length: count }, (_, i) => `line-${i}`).join("\n")}\n`,
    );
  }

  it("opens scrolled to the end, where the failure is", async () => {
    mockApi.get.mockResolvedValue(logLines(50));
    renderLog();
    open();

    const view = await screen.findByRole("log");
    expect(view.scrollTop).toBe(50 * LINE_PX);
  });
});
