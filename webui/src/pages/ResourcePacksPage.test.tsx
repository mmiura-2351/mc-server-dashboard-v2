import {
  act,
  fireEvent,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "../auth/tokenStore.ts";
import {
  _resetForTesting as resetUploadProgress,
  useUploadProgress,
} from "../components/useUploadProgress.ts";
import { t } from "../i18n/index.ts";
import { installMockXhrUpload } from "../test/mockXhrUpload.ts";
import { renderApp } from "../test/render.tsx";

// Resource packs library page (#1178). Driven through the real router +
// providers via renderApp; a fetch mock dispatches on URL + method so a single
// case can stand up bootstrap (/users/me, /communities, permissions) plus the
// resource pack list, upload, download, and delete.

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function emptyResponse(status = 204): Response {
  return new Response(null, { status });
}

function errorResponse(status: number, reason?: string): Response {
  return new Response(JSON.stringify({ reason }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function tokenResponse(): Response {
  return jsonResponse({
    access_token: "fresh",
    token_type: "bearer",
  });
}

const fetchMock = vi.fn();

const ADMIN = {
  id: "u1",
  username: "admin",
  email: "admin@example.com",
  is_platform_admin: true,
};

const NON_ADMIN = {
  id: "u2",
  username: "member",
  email: "member@example.com",
  is_platform_admin: false,
};

const PACKS = {
  resource_packs: [
    {
      id: "rp1",
      display_name: "Faithful",
      filename: "faithful-32x.zip",
      size_bytes: 10_485_760,
      sha1_hash: "abc123def456",
      sha256_hash: "abc123def456abc123def456",
      created_at: "2026-06-10T10:00:00Z",
      updated_at: "2026-06-10T10:00:00Z",
      uploaded_by: "u1",
      download_url: "/api/resource-packs/rp1/download",
      description: null,
    },
    {
      id: "rp2",
      display_name: "Sphax",
      filename: "sphax-128x.zip",
      size_bytes: 52_428_800,
      sha1_hash: "789abc012def",
      sha256_hash: "789abc012def789abc012def",
      created_at: "2026-06-11T14:30:00Z",
      updated_at: "2026-06-11T14:30:00Z",
      uploaded_by: "u2",
      download_url: "/api/resource-packs/rp2/download",
      description: "High-res pack",
    },
  ],
};

const EMPTY_PACKS = { resource_packs: [] };

let calls: { url: string; method: string }[] = [];

interface MockOverrides {
  user?: typeof ADMIN | typeof NON_ADMIN;
  packs?: typeof PACKS | typeof EMPTY_PACKS;
  listError?: boolean;
  uploadError?: boolean;
  deleteError?: boolean;
  deleteInUse?: boolean;
}

function signedIn(overrides: MockOverrides = {}) {
  const user = overrides.user ?? ADMIN;
  const packs = overrides.packs ?? PACKS;

  fetchMock.mockImplementation(
    (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ url, method });

      if (url === "/api/users/me") return Promise.resolve(jsonResponse(user));
      if (url === "/api/communities")
        return Promise.resolve(jsonResponse([{ id: "c1", name: "Alpha" }]));
      if (url.endsWith("/me/permissions"))
        return Promise.resolve(jsonResponse({}));

      if (url === "/api/resource-packs" && method === "GET") {
        return Promise.resolve(
          overrides.listError ? errorResponse(503) : jsonResponse(packs),
        );
      }

      if (url === "/api/resource-packs" && method === "POST") {
        return Promise.resolve(
          overrides.uploadError
            ? errorResponse(500)
            : jsonResponse(
                {
                  id: "rp-new",
                  display_name: "New Pack",
                  filename: "newpack.zip",
                  size_bytes: 1024,
                  sha1_hash: "newsha1",
                  sha256_hash: "newsha256",
                  created_at: "2026-06-12T00:00:00Z",
                  updated_at: "2026-06-12T00:00:00Z",
                  uploaded_by: user.id,
                  download_url: "/api/resource-packs/rp-new/download",
                  description: null,
                },
                201,
              ),
        );
      }

      if (url.match(/\/api\/resource-packs\/[^/]+$/) && method === "DELETE") {
        if (overrides.deleteInUse) {
          return Promise.resolve(errorResponse(409, "resource_pack_in_use"));
        }
        return Promise.resolve(
          overrides.deleteError ? errorResponse(500) : emptyResponse(),
        );
      }

      // Download endpoint — return a blob-like response.
      if (url.match(/\/api\/resource-packs\/[^/]+\/download/)) {
        return Promise.resolve(
          new Response(new Blob(["fake-zip"]), {
            status: 200,
            headers: { "content-type": "application/zip" },
          }),
        );
      }

      return Promise.resolve(tokenResponse());
    },
  );
}

let restoreXhr: () => void;
beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  calls = [];
  clearAccessToken();
  // Uploads go through postFormWithProgress (XHR, #1207); route XHR through the
  // same fetch dispatcher so the upload hits the mocked POST handler.
  restoreXhr = installMockXhrUpload(fetchMock);
});

afterEach(() => {
  restoreXhr();
  vi.unstubAllGlobals();
});

describe("resource packs library", () => {
  it("renders the list with pack names, filenames, and sizes", async () => {
    signedIn();

    renderApp({ path: "/resource-packs" });

    expect(await screen.findByText("Faithful")).toBeInTheDocument();
    expect(screen.getByText("faithful-32x.zip")).toBeInTheDocument();
    expect(screen.getByText("10.0 MiB")).toBeInTheDocument();

    expect(screen.getByText("Sphax")).toBeInTheDocument();
    expect(screen.getByText("sphax-128x.zip")).toBeInTheDocument();
    expect(screen.getByText("50.0 MiB")).toBeInTheDocument();
  });

  it("shows the empty state when there are no packs", async () => {
    signedIn({ packs: EMPTY_PACKS });

    renderApp({ path: "/resource-packs" });

    expect(
      await screen.findByText(t("resourcePacks.empty")),
    ).toBeInTheDocument();
  });

  it("shows an error when the list fails to load", async () => {
    signedIn({ listError: true });

    renderApp({ path: "/resource-packs" });

    expect(
      await screen.findByText(t("resourcePacks.loadError")),
    ).toBeInTheDocument();
  });

  it("shows delete button only for own packs when not admin", async () => {
    signedIn({ user: NON_ADMIN });

    renderApp({ path: "/resource-packs" });

    // Wait for the list to render.
    await screen.findByText("Faithful");

    // NON_ADMIN (u2) uploaded Sphax (rp2), so its row should have a delete button.
    // Faithful was uploaded by u1, so no delete button for non-admin.
    const deleteButtons = screen.getAllByRole("button", {
      name: t("resourcePacks.delete"),
    });
    // Only one delete button — the one for Sphax (u2's pack).
    expect(deleteButtons).toHaveLength(1);
  });

  it("shows delete buttons for all packs when admin", async () => {
    signedIn({ user: ADMIN });

    renderApp({ path: "/resource-packs" });

    await screen.findByText("Faithful");

    const deleteButtons = screen.getAllByRole("button", {
      name: t("resourcePacks.delete"),
    });
    expect(deleteButtons).toHaveLength(2);
  });

  it("uploads a resource pack through the dialog", async () => {
    signedIn();

    renderApp({ path: "/resource-packs" });

    const uploadButton = await screen.findByRole("button", {
      name: t("resourcePacks.upload"),
    });
    fireEvent.click(uploadButton);

    // Fill display name.
    const nameInput = await screen.findByRole("textbox");
    fireEvent.change(nameInput, { target: { value: "My Pack" } });

    // Select file.
    const fileInput = screen.getByLabelText(
      t("common.chooseFile"),
    ) as HTMLInputElement;
    const file = new File(["content"], "mypack.zip", {
      type: "application/zip",
    });
    fireEvent.change(fileInput, { target: { files: [file] } });

    // Submit.
    const submitButton = screen.getByRole("button", {
      name: t("resourcePacks.uploadDialog.submit"),
    });
    fireEvent.click(submitButton);

    await waitFor(() => {
      expect(
        calls.some(
          (c) => c.method === "POST" && c.url === "/api/resource-packs",
        ),
      ).toBe(true);
    });

    expect(
      await screen.findByText(t("resourcePacks.uploaded")),
    ).toBeInTheDocument();
  });

  it("resets upload progress on successful upload (#1984)", async () => {
    resetUploadProgress();
    signedIn();

    renderApp({ path: "/resource-packs" });

    const uploadButton = await screen.findByRole("button", {
      name: t("resourcePacks.upload"),
    });
    fireEvent.click(uploadButton);

    const nameInput = await screen.findByRole("textbox");
    fireEvent.change(nameInput, { target: { value: "My Pack" } });

    const fileInput = screen.getByLabelText(
      t("common.chooseFile"),
    ) as HTMLInputElement;
    const file = new File(["content"], "mypack.zip", {
      type: "application/zip",
    });
    fireEvent.change(fileInput, { target: { files: [file] } });

    const submitButton = screen.getByRole("button", {
      name: t("resourcePacks.uploadDialog.submit"),
    });
    fireEvent.click(submitButton);

    await screen.findByText(t("resourcePacks.uploaded"));

    // The shared upload-progress singleton must be idle after a successful
    // upload — otherwise other upload surfaces show a stale progress bar.
    const { result } = renderHook(() => useUploadProgress());
    expect(result.current.active).toBe(false);
  });

  it("shows an error toast when upload fails", async () => {
    signedIn({ uploadError: true });

    renderApp({ path: "/resource-packs" });

    const uploadButton = await screen.findByRole("button", {
      name: t("resourcePacks.upload"),
    });
    fireEvent.click(uploadButton);

    const nameInput = await screen.findByRole("textbox");
    fireEvent.change(nameInput, { target: { value: "Bad Pack" } });

    const fileInput = screen.getByLabelText(
      t("common.chooseFile"),
    ) as HTMLInputElement;
    const file = new File(["content"], "bad.zip", {
      type: "application/zip",
    });
    fireEvent.change(fileInput, { target: { files: [file] } });

    const submitButton = screen.getByRole("button", {
      name: t("resourcePacks.uploadDialog.submit"),
    });
    fireEvent.click(submitButton);

    expect(
      await screen.findByText(t("resourcePacks.error.uploadFailed")),
    ).toBeInTheDocument();
  });

  it("deletes a resource pack after typed confirmation", async () => {
    signedIn();

    renderApp({ path: "/resource-packs" });

    await screen.findByText("Faithful");

    // Click the first delete button (Faithful).
    const deleteButtons = screen.getAllByRole("button", {
      name: t("resourcePacks.delete"),
    });
    fireEvent.click(deleteButtons[0]);

    // Type the confirm phrase (the display name).
    const input = await screen.findByPlaceholderText("Faithful");
    fireEvent.change(input, { target: { value: "Faithful" } });

    // Confirm.
    fireEvent.click(
      screen.getByRole("button", {
        name: t("resourcePacks.deleteDialog.confirm"),
      }),
    );

    await waitFor(() => {
      expect(
        calls.some(
          (c) => c.method === "DELETE" && c.url === "/api/resource-packs/rp1",
        ),
      ).toBe(true);
    });

    expect(
      await screen.findByText(t("resourcePacks.deleted")),
    ).toBeInTheDocument();
  });

  it("handles 409 resource_pack_in_use on delete", async () => {
    signedIn({ deleteInUse: true });

    renderApp({ path: "/resource-packs" });

    await screen.findByText("Faithful");

    const deleteButtons = screen.getAllByRole("button", {
      name: t("resourcePacks.delete"),
    });
    fireEvent.click(deleteButtons[0]);

    const input = await screen.findByPlaceholderText("Faithful");
    fireEvent.change(input, { target: { value: "Faithful" } });

    fireEvent.click(
      screen.getByRole("button", {
        name: t("resourcePacks.deleteDialog.confirm"),
      }),
    );

    expect(
      await screen.findByText(t("resourcePacks.error.inUse")),
    ).toBeInTheDocument();
  });

  it("keeps rendering cached packs when a background refetch fails (#1805)", async () => {
    signedIn();
    const { queryClient } = renderApp({ path: "/resource-packs" });
    await screen.findByText("Faithful");

    // Simulate a transient API outage: resource packs endpoint fails.
    signedIn({ listError: true });
    await act(() => queryClient.invalidateQueries());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // The cached list stays on screen instead of the error.
    expect(screen.getByText("Faithful")).toBeInTheDocument();
    expect(
      screen.queryByText(t("resourcePacks.loadError")),
    ).not.toBeInTheDocument();
  });
});
