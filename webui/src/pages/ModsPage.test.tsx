import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "../auth/tokenStore.ts";
import { t } from "../i18n/index.ts";
import { renderApp } from "../test/render.tsx";

// Mod library page (#1266). Driven through the real router + providers via
// renderApp; a fetch mock dispatches on URL + method so a single case can stand
// up bootstrap (/users/me, /communities, permissions) plus the mod list,
// upload, delete, download, and the Modrinth search/import modal. Mirrors the
// resource pack page test (ResourcePacksPage.test.tsx).

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
    refresh_token: "ignored",
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

function mod(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "m1",
    display_name: "Sodium",
    filename: "sodium-0.5.jar",
    description: null,
    loader_type: "fabric",
    mod_identifier: "sodium",
    provides: [],
    version_number: "0.5.3",
    mc_versions: ["1.20.1"],
    side: "client",
    dependencies: [],
    sha256_hash: "abc123",
    sha512_hash: null,
    size_bytes: 1_048_576,
    source: "local",
    uploaded_by: "u1",
    created_at: "2026-06-10T10:00:00Z",
    updated_at: "2026-06-10T10:00:00Z",
    ...over,
  };
}

const MODS = {
  mods: [
    mod(),
    mod({
      id: "m2",
      display_name: "Fabric API",
      filename: "fabric-api-0.92.jar",
      mod_identifier: "fabric",
      version_number: "0.92.0",
      mc_versions: ["1.20.1"],
      side: "both",
      uploaded_by: "u2",
    }),
  ],
};

const EMPTY_MODS = { mods: [] };

const SEARCH = {
  total: 1,
  hits: [
    {
      project_id: "p1",
      slug: "sodium",
      title: "Sodium",
      description: "A rendering optimization mod.",
      project_type: "mod",
      side: "client",
      loaders: ["fabric"],
      game_versions: ["1.20.1"],
      icon_url: null,
      downloads: 1000,
    },
  ],
};

const PROJECT = {
  project_id: "p1",
  slug: "sodium",
  title: "Sodium",
  description: "A rendering optimization mod.",
  project_type: "mod",
  side: "client",
  loaders: ["fabric"],
  game_versions: ["1.20.1"],
  versions: [
    {
      version_id: "v1",
      project_id: "p1",
      name: "Sodium 0.5.3",
      version_number: "0.5.3",
      filename: "sodium-0.5.3.jar",
      download_url: "https://example.test/sodium.jar",
      sha512: null,
      loaders: ["fabric"],
      game_versions: ["1.20.1"],
      dependencies: [],
    },
  ],
};

let calls: { url: string; method: string }[] = [];

interface MockOverrides {
  user?: typeof ADMIN | typeof NON_ADMIN;
  mods?: typeof MODS | typeof EMPTY_MODS;
  listError?: boolean;
  uploadError?: boolean;
  deleteInUse?: boolean;
  searchError?: boolean;
  importError?: boolean;
}

function signedIn(overrides: MockOverrides = {}) {
  const user = overrides.user ?? ADMIN;
  const mods = overrides.mods ?? MODS;

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

      if (url === "/api/mods" && method === "GET") {
        return Promise.resolve(
          overrides.listError ? errorResponse(503) : jsonResponse(mods),
        );
      }

      if (url === "/api/mods" && method === "POST") {
        return Promise.resolve(
          overrides.uploadError
            ? errorResponse(500)
            : jsonResponse(mod({ id: "m-new", display_name: "New Mod" }), 201),
        );
      }

      if (url.match(/\/api\/mods\/[^/]+$/) && method === "DELETE") {
        if (overrides.deleteInUse) {
          return Promise.resolve(errorResponse(409, "mod_in_use"));
        }
        return Promise.resolve(emptyResponse());
      }

      if (url.match(/\/api\/mods\/[^/]+\/download/)) {
        return Promise.resolve(
          new Response(new Blob(["fake-jar"]), {
            status: 200,
            headers: { "content-type": "application/java-archive" },
          }),
        );
      }

      if (url.startsWith("/api/catalog/search")) {
        return Promise.resolve(
          overrides.searchError ? errorResponse(502) : jsonResponse(SEARCH),
        );
      }

      if (url.startsWith("/api/catalog/projects/")) {
        return Promise.resolve(jsonResponse(PROJECT));
      }

      if (url === "/api/mods/import" && method === "POST") {
        return Promise.resolve(
          overrides.importError
            ? errorResponse(502)
            : jsonResponse(mod({ id: "m-imported" }), 201),
        );
      }

      return Promise.resolve(tokenResponse());
    },
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  calls = [];
  clearAccessToken();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("mod library", () => {
  it("renders the list with names, versions, loaders, and a side badge", async () => {
    signedIn();

    renderApp({ path: "/mods" });

    expect(await screen.findByText("Sodium")).toBeInTheDocument();
    expect(screen.getByText("0.5.3")).toBeInTheDocument();
    expect(screen.getAllByText("fabric").length).toBeGreaterThan(0);
    // Side badge: m1 is client, m2 is both.
    expect(screen.getByText(t("mods.side.client"))).toBeInTheDocument();
    expect(screen.getByText(t("mods.side.both"))).toBeInTheDocument();
  });

  it("shows the empty state when there are no mods", async () => {
    signedIn({ mods: EMPTY_MODS });

    renderApp({ path: "/mods" });

    expect(await screen.findByText(t("mods.empty"))).toBeInTheDocument();
  });

  it("shows an error when the list fails to load", async () => {
    signedIn({ listError: true });

    renderApp({ path: "/mods" });

    expect(await screen.findByText(t("mods.loadError"))).toBeInTheDocument();
  });

  it("shows delete button only for own mods when not admin", async () => {
    signedIn({ user: NON_ADMIN });

    renderApp({ path: "/mods" });

    await screen.findByText("Sodium");

    // NON_ADMIN (u2) uploaded Fabric API (m2); Sodium (m1) is u1's.
    const deleteButtons = screen.getAllByRole("button", {
      name: t("mods.delete"),
    });
    expect(deleteButtons).toHaveLength(1);
  });

  it("shows delete buttons for all mods when admin", async () => {
    signedIn({ user: ADMIN });

    renderApp({ path: "/mods" });

    await screen.findByText("Sodium");

    const deleteButtons = screen.getAllByRole("button", {
      name: t("mods.delete"),
    });
    expect(deleteButtons).toHaveLength(2);
  });

  it("uploads a mod through the dialog", async () => {
    signedIn();

    renderApp({ path: "/mods" });

    fireEvent.click(
      await screen.findByRole("button", { name: t("mods.upload") }),
    );

    const nameInput = await screen.findByRole("textbox");
    fireEvent.change(nameInput, { target: { value: "My Mod" } });

    const fileInput = screen.getByLabelText(
      t("common.chooseFile"),
    ) as HTMLInputElement;
    const file = new File(["content"], "mymod.jar", {
      type: "application/java-archive",
    });
    fireEvent.change(fileInput, { target: { files: [file] } });

    fireEvent.click(
      screen.getByRole("button", { name: t("mods.uploadDialog.submit") }),
    );

    await waitFor(() => {
      expect(
        calls.some((c) => c.method === "POST" && c.url === "/api/mods"),
      ).toBe(true);
    });

    expect(await screen.findByText(t("mods.uploaded"))).toBeInTheDocument();
  });

  it("shows an error toast when upload fails", async () => {
    signedIn({ uploadError: true });

    renderApp({ path: "/mods" });

    fireEvent.click(
      await screen.findByRole("button", { name: t("mods.upload") }),
    );

    fireEvent.change(await screen.findByRole("textbox"), {
      target: { value: "Bad Mod" },
    });

    const fileInput = screen.getByLabelText(
      t("common.chooseFile"),
    ) as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: {
        files: [
          new File(["x"], "bad.jar", { type: "application/java-archive" }),
        ],
      },
    });

    fireEvent.click(
      screen.getByRole("button", { name: t("mods.uploadDialog.submit") }),
    );

    expect(
      await screen.findByText(t("mods.error.uploadFailed")),
    ).toBeInTheDocument();
  });

  it("deletes a mod after typed confirmation", async () => {
    signedIn();

    renderApp({ path: "/mods" });

    await screen.findByText("Sodium");

    fireEvent.click(
      screen.getAllByRole("button", { name: t("mods.delete") })[0],
    );

    const input = await screen.findByPlaceholderText("Sodium");
    fireEvent.change(input, { target: { value: "Sodium" } });

    fireEvent.click(
      screen.getByRole("button", { name: t("mods.deleteDialog.confirm") }),
    );

    await waitFor(() => {
      expect(
        calls.some((c) => c.method === "DELETE" && c.url === "/api/mods/m1"),
      ).toBe(true);
    });

    expect(await screen.findByText(t("mods.deleted"))).toBeInTheDocument();
  });

  it("handles 409 mod_in_use on delete", async () => {
    signedIn({ deleteInUse: true });

    renderApp({ path: "/mods" });

    await screen.findByText("Sodium");

    fireEvent.click(
      screen.getAllByRole("button", { name: t("mods.delete") })[0],
    );

    const input = await screen.findByPlaceholderText("Sodium");
    fireEvent.change(input, { target: { value: "Sodium" } });

    fireEvent.click(
      screen.getByRole("button", { name: t("mods.deleteDialog.confirm") }),
    );

    expect(await screen.findByText(t("mods.error.inUse"))).toBeInTheDocument();
  });

  it("searches Modrinth and imports a version into the library", async () => {
    signedIn();

    renderApp({ path: "/mods" });

    // Open the Modrinth modal.
    fireEvent.click(
      await screen.findByRole("button", { name: t("mods.browse") }),
    );

    // Type a query and search.
    fireEvent.change(
      await screen.findByPlaceholderText(
        t("mods.browseDialog.queryPlaceholder"),
      ),
      { target: { value: "sodium" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("mods.browseDialog.search") }),
    );

    // Result hit shows; clicking View versions drills into the project versions.
    await screen.findByText("A rendering optimization mod.");
    expect(calls.some((c) => c.url.startsWith("/api/catalog/search"))).toBe(
      true,
    );

    fireEvent.click(
      screen.getByRole("button", {
        name: t("mods.browseDialog.viewVersions"),
      }),
    );

    // Project detail loads its versions; import the first version.
    await screen.findByText("Sodium 0.5.3");
    expect(calls.some((c) => c.url.startsWith("/api/catalog/projects/"))).toBe(
      true,
    );

    fireEvent.click(
      screen.getByRole("button", { name: t("mods.browseDialog.import") }),
    );

    await waitFor(() => {
      expect(
        calls.some((c) => c.method === "POST" && c.url === "/api/mods/import"),
      ).toBe(true);
    });

    expect(
      await screen.findByText(t("mods.browseDialog.imported")),
    ).toBeInTheDocument();
  });

  it("shows an error when the Modrinth search fails", async () => {
    signedIn({ searchError: true });

    renderApp({ path: "/mods" });

    fireEvent.click(
      await screen.findByRole("button", { name: t("mods.browse") }),
    );

    fireEvent.change(
      await screen.findByPlaceholderText(
        t("mods.browseDialog.queryPlaceholder"),
      ),
      { target: { value: "sodium" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: t("mods.browseDialog.search") }),
    );

    expect(
      await screen.findByText(t("mods.browseDialog.searchFailed")),
    ).toBeInTheDocument();
  });
});
