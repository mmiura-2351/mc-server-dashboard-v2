import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "../auth/tokenStore.ts";
import { t } from "../i18n/index.ts";
import { renderApp } from "../test/render.tsx";

// Gating + Overview tests (#474). The app is driven through the real router and
// providers via renderApp; a fetch mock dispatches on URL so a single test can
// stand up /users/me, the community list, and the platform-admin `[A]`
// endpoints the Overview reads.

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
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
const MEMBER = { ...ADMIN, is_platform_admin: false };

// Pinned "now" so heartbeatAge calls in the rendered table are deterministic
// under load — same technique as AdminWorkersPage (#817).
const NOW = new Date("2026-01-01T00:00:00Z");

const WORKERS = {
  workers: [
    {
      id: "worker-a",
      version: "0.9.2",
      status: "online",
      assigned_count: 2,
      last_heartbeat_at: NOW.toISOString(),
      registered_at: NOW.toISOString(),
      capabilities: { drivers: ["container"], max_servers: 8, resources: {} },
    },
    {
      id: "worker-b",
      version: "0.9.2",
      status: "draining",
      assigned_count: 1,
      last_heartbeat_at: NOW.toISOString(),
      registered_at: NOW.toISOString(),
      capabilities: { drivers: ["container"], max_servers: 4, resources: {} },
    },
    {
      id: "worker-c",
      version: "0.9.2",
      status: "offline",
      assigned_count: 0,
      last_heartbeat_at: new Date(NOW.getTime() - 3_600_000).toISOString(),
      registered_at: NOW.toISOString(),
      capabilities: { drivers: ["container"], max_servers: 4, resources: {} },
    },
  ],
};

const BACKUP_STATS = {
  count: 31,
  newest: null,
  oldest: null,
  total_bytes: 13_314_398_421,
  unknown_size_count: 0,
};

const JAR_POOL = { count: 14, total_bytes: 2_040_109_466 };

// Wire the fetch mock for a signed-in user with the given admin flag. The
// bootstrap refresh and any unmatched URL fall through to a token response.
function signedInAs(user: typeof ADMIN) {
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url === "/api/users/me") return Promise.resolve(jsonResponse(user));
    if (url === "/api/communities")
      return Promise.resolve(jsonResponse([{ id: "c1", name: "Alpha" }]));
    if (url.endsWith("/me/permissions"))
      return Promise.resolve(jsonResponse({}));
    if (url === "/api/workers") return Promise.resolve(jsonResponse(WORKERS));
    if (url === "/api/backups/statistics")
      return Promise.resolve(jsonResponse(BACKUP_STATS));
    if (url === "/api/versions/jar-pool/stats")
      return Promise.resolve(jsonResponse(JAR_POOL));
    return Promise.resolve(tokenResponse());
  });
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  clearAccessToken();
});

afterEach(() => {
  vi.unstubAllGlobals();
  // Clear any pending fake timers (e.g. react-query refetchInterval) before
  // restoring the real clock so vitest worker teardown does not time out (#817).
  vi.clearAllTimers();
  vi.useRealTimers();
});

describe("admin gating", () => {
  it("hides the admin nav group and denies /admin for a non-admin", async () => {
    signedInAs(MEMBER);

    renderApp({ path: "/admin" });

    expect(
      await screen.findByText(t("admin.denied.title")),
    ).toBeInTheDocument();
    expect(screen.queryByText(t("nav.admin"))).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: t("nav.adminOverview") }),
    ).not.toBeInTheDocument();
  });

  it("shows the admin nav group and renders the Overview for an admin", async () => {
    signedInAs(ADMIN);

    renderApp({ path: "/admin" });

    expect(await screen.findByText(t("nav.admin"))).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: t("nav.adminOverview") }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: t("page.adminOverview") }),
    ).toBeInTheDocument();
    expect(screen.queryByText(t("admin.denied.title"))).not.toBeInTheDocument();
  });
});

describe("admin overview stats", () => {
  it("derives worker, server, backup and jar-pool stats from the endpoints", async () => {
    signedInAs(ADMIN);

    renderApp({ path: "/admin" });

    // Worker tile: 1 online / 3 total, with 1 draining · 1 offline. Scope by the
    // metric-tile label so the nav "Workers" link does not collide.
    const tileByLabel = (label: string): Element => {
      const tiles = Array.from(document.querySelectorAll(".metric-tile"));
      const tile = tiles.find(
        (el) => el.querySelector(".label")?.textContent === label,
      );
      if (tile === undefined) throw new Error(`no tile labelled ${label}`);
      return tile;
    };

    // Wait for the stats to load: the fleet heading only renders once Loaded.
    await screen.findByRole("heading", { name: t("admin.overview.fleet") });
    const workersTile = tileByLabel(t("admin.overview.workers"));
    expect(workersTile.querySelector(".value")?.textContent).toBe(
      `1 / 3 ${t("admin.overview.workersOnline")}`,
    );
    expect(
      screen.getByText(
        `1 ${t("admin.overview.workersDraining")} · 1 ${t("admin.overview.workersOffline")}`,
      ),
    ).toBeInTheDocument();

    // Servers running = sum of assigned_count (2 + 1 + 0 = 3).
    const serversTile = tileByLabel(t("admin.overview.servers"));
    expect(serversTile.querySelector(".value")?.textContent).toBe("3");

    // Backup count + size and jar-pool stats render.
    const backupsTile = tileByLabel(t("admin.overview.backups"));
    expect(backupsTile.querySelector(".value")?.textContent).toBe(
      "31 · 12.4 GiB",
    );
    expect(backupsTile.querySelector(".hint")?.textContent).toBe(
      `${t("admin.overview.jarPool")}: 14 ${t("admin.overview.jars")} · 1.9 GiB`,
    );

    // Worker fleet table lists every worker with its status pill.
    expect(screen.getByText("worker-a")).toBeInTheDocument();
    expect(screen.getByText("worker-b")).toBeInTheDocument();
    expect(screen.getByText("worker-c")).toBeInTheDocument();
  });

  it("shows the error state when a stats endpoint fails", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/users/me") return Promise.resolve(jsonResponse(ADMIN));
      if (url === "/api/communities")
        return Promise.resolve(jsonResponse([{ id: "c1", name: "Alpha" }]));
      if (url.endsWith("/me/permissions"))
        return Promise.resolve(jsonResponse({}));
      if (url === "/api/workers")
        return Promise.resolve(
          new Response("nope", {
            status: 503,
            headers: { "content-type": "application/json" },
          }),
        );
      return Promise.resolve(tokenResponse());
    });

    renderApp({ path: "/admin" });

    expect(
      await screen.findByText(t("admin.overview.loadError")),
    ).toBeInTheDocument();
  });

  it("re-fetches worker data after 12 s so heartbeat ages do not freeze (#791)", async () => {
    // shouldAdvanceTime lets react-query timers fire while async utilities still
    // resolve (mirrors the AdminWorkersPage timer pattern). Clock is restored in
    // afterEach along with vi.clearAllTimers() to prevent timer leaks (#817).
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW);
    signedInAs(ADMIN);

    renderApp({ path: "/admin" });

    // Wait for the initial render to settle.
    await screen.findByRole("heading", { name: t("admin.overview.fleet") });

    const callsBefore = fetchMock.mock.calls.filter((c) => {
      const url =
        typeof c[0] === "string"
          ? c[0]
          : (c[0] as { toString(): string }).toString();
      return url === "/api/workers";
    }).length;

    vi.advanceTimersByTime(12_000);

    await screen.findByRole("heading", { name: t("admin.overview.fleet") });

    const callsAfter = fetchMock.mock.calls.filter((c) => {
      const url =
        typeof c[0] === "string"
          ? c[0]
          : (c[0] as { toString(): string }).toString();
      return url === "/api/workers";
    }).length;

    expect(callsAfter).toBeGreaterThan(callsBefore);
  });
});
