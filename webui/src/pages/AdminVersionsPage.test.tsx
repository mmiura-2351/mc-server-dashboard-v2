import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "../auth/tokenStore.ts";
import { t } from "../i18n/index.ts";
import { renderApp } from "../test/render.tsx";

// Admin Versions page (#478). Driven through the real router + providers via
// renderApp; a fetch mock dispatches on URL + method so a single case can stand
// up bootstrap (/users/me, /communities, permissions) plus the version catalog,
// per-type listings, JAR-pool stats, refresh and GC.

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function errorResponse(status: number): Response {
  return new Response(JSON.stringify({ reason: "boom" }), {
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

const TYPES = { server_types: ["vanilla", "paper"] };
const VANILLA = { versions: ["1.21.4", "1.21.3", "1.20.6"] };
const PAPER = { versions: ["1.21.4", "1.21.1"] };
const JAR_POOL = { count: 14, total_bytes: 2_040_109_466 };
const GC_RESULT = { scanned: 14, deleted: 3, freed_bytes: 432_013_312 };

// Capture the requests the page issues so the tests can assert refresh/GC shapes.
let calls: { url: string; method: string }[] = [];

interface MockOverrides {
  versionsError?: boolean;
  jarStatsError?: boolean;
  refreshError?: boolean;
  gcError?: boolean;
}

function signedIn(overrides: MockOverrides = {}) {
  fetchMock.mockImplementation(
    (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ url, method });

      if (url === "/users/me") return Promise.resolve(jsonResponse(ADMIN));
      if (url === "/communities")
        return Promise.resolve(jsonResponse([{ id: "c1", name: "Alpha" }]));
      if (url.endsWith("/me/permissions"))
        return Promise.resolve(jsonResponse({}));

      if (url === "/versions" && method === "GET") {
        return Promise.resolve(
          overrides.versionsError ? errorResponse(503) : jsonResponse(TYPES),
        );
      }
      if (url === "/versions/vanilla")
        return Promise.resolve(jsonResponse(VANILLA));
      if (url === "/versions/paper")
        return Promise.resolve(jsonResponse(PAPER));

      if (url.startsWith("/versions/refresh")) {
        return Promise.resolve(
          overrides.refreshError
            ? errorResponse(500)
            : jsonResponse({ invalidated: ["vanilla", "paper"] }),
        );
      }

      if (url === "/versions/jar-pool/stats") {
        return Promise.resolve(
          overrides.jarStatsError ? errorResponse(503) : jsonResponse(JAR_POOL),
        );
      }
      if (url === "/versions/jar-pool/gc") {
        return Promise.resolve(
          overrides.gcError ? errorResponse(500) : jsonResponse(GC_RESULT),
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

describe("admin versions catalog", () => {
  it("renders each type with its version count and latest", async () => {
    signedIn();

    renderApp({ path: "/admin/versions" });

    const vanillaRow = (await screen.findByText("vanilla")).closest("tr");
    if (vanillaRow === null) throw new Error("no vanilla row");
    // The per-type version lists load after the type list, so wait for the count.
    expect(await within(vanillaRow).findByText("3")).toBeInTheDocument();
    expect(within(vanillaRow).getByText("1.21.4")).toBeInTheDocument();

    const paperRow = screen.getByText("paper").closest("tr");
    if (paperRow === null) throw new Error("no paper row");
    expect(within(paperRow).getByText("2")).toBeInTheDocument();
  });

  it("refreshes all catalogs with no server_type query", async () => {
    signedIn();

    renderApp({ path: "/admin/versions" });

    const button = await screen.findByRole("button", {
      name: t("admin.versions.refreshAll"),
    });
    fireEvent.click(button);

    await waitFor(() => {
      expect(
        calls.some((c) => c.method === "POST" && c.url === "/versions/refresh"),
      ).toBe(true);
    });
    expect(
      await screen.findByText(t("admin.versions.refreshedAll")),
    ).toBeInTheDocument();
  });

  it("refreshes a single type with its server_type query", async () => {
    signedIn();

    renderApp({ path: "/admin/versions" });

    const paperRow = (await screen.findByText("paper")).closest("tr");
    if (paperRow === null) throw new Error("no paper row");
    fireEvent.click(
      within(paperRow).getByRole("button", {
        name: t("admin.versions.refresh"),
      }),
    );

    await waitFor(() => {
      expect(
        calls.some(
          (c) =>
            c.method === "POST" &&
            c.url === "/versions/refresh?server_type=paper",
        ),
      ).toBe(true);
    });
    expect(
      await screen.findByText(`${t("admin.versions.refreshedOne")}paper`),
    ).toBeInTheDocument();
  });

  it("shows the error state when the type catalog fails to load", async () => {
    signedIn({ versionsError: true });

    renderApp({ path: "/admin/versions" });

    expect(
      await screen.findByText(t("admin.versions.loadError")),
    ).toBeInTheDocument();
  });

  it("surfaces an error toast when a refresh fails", async () => {
    signedIn({ refreshError: true });

    renderApp({ path: "/admin/versions" });

    const button = await screen.findByRole("button", {
      name: t("admin.versions.refreshAll"),
    });
    fireEvent.click(button);

    expect(
      await screen.findByText(t("admin.versions.refreshError")),
    ).toBeInTheDocument();
  });
});

describe("admin versions JAR pool", () => {
  it("displays the pool count and humanized size", async () => {
    signedIn();

    renderApp({ path: "/admin/versions" });

    const poolHeading = await screen.findByRole("heading", {
      name: t("admin.versions.jarPool"),
    });
    const poolCard = poolHeading.closest<HTMLElement>(".jar-pool");
    if (poolCard === null) throw new Error("no jar-pool card");
    // Stats load after the heading renders; wait for the count.
    expect(await within(poolCard).findByText("14")).toBeInTheDocument();
    expect(within(poolCard).getByText("1.9 GiB")).toBeInTheDocument();
  });

  it("runs GC after confirmation and reports reclaimed bytes", async () => {
    signedIn();

    renderApp({ path: "/admin/versions" });

    const gcButton = await screen.findByRole("button", {
      name: t("admin.versions.gc"),
    });
    fireEvent.click(gcButton);

    // Typed-confirm: the confirm button enables only after typing the phrase.
    const input = await screen.findByPlaceholderText("GC");
    fireEvent.change(input, { target: { value: "GC" } });
    fireEvent.click(
      screen.getByRole("button", {
        name: t("admin.versions.gcDialog.confirm"),
      }),
    );

    await waitFor(() => {
      expect(
        calls.some(
          (c) => c.method === "POST" && c.url === "/versions/jar-pool/gc",
        ),
      ).toBe(true);
    });
    // freed_bytes 432013312 → "412.0 MiB"; deleted 3.
    expect(
      await screen.findByText(
        t("admin.versions.gcDoneReclaimed") +
          "412.0 MiB" +
          t("admin.versions.gcDoneAcross") +
          "3" +
          t("admin.versions.gcDoneJars"),
      ),
    ).toBeInTheDocument();
  });

  it("surfaces an error toast when GC fails", async () => {
    signedIn({ gcError: true });

    renderApp({ path: "/admin/versions" });

    const gcButton = await screen.findByRole("button", {
      name: t("admin.versions.gc"),
    });
    fireEvent.click(gcButton);
    const input = await screen.findByPlaceholderText("GC");
    fireEvent.change(input, { target: { value: "GC" } });
    fireEvent.click(
      screen.getByRole("button", {
        name: t("admin.versions.gcDialog.confirm"),
      }),
    );

    expect(
      await screen.findByText(t("admin.versions.gcError")),
    ).toBeInTheDocument();
  });

  it("shows the error state when the JAR pool stats fail to load", async () => {
    signedIn({ jarStatsError: true });

    renderApp({ path: "/admin/versions" });

    // The pool card renders its own load error independent of the catalog.
    const poolHeading = await screen.findByRole("heading", {
      name: t("admin.versions.jarPool"),
    });
    const poolCard = poolHeading.closest<HTMLElement>(".jar-pool");
    if (poolCard === null) throw new Error("no jar-pool card");
    expect(
      await within(poolCard).findByText(t("admin.versions.loadError")),
    ).toBeInTheDocument();
  });
});
