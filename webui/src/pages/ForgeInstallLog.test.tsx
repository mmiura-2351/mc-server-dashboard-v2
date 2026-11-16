import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
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
