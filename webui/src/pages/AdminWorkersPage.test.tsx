import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccessToken } from "../auth/tokenStore.ts";
import { t } from "../i18n/index.ts";
import { renderApp } from "../test/render.tsx";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
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

// Seed heartbeat fixtures from a fixed reference timestamp.
const NOW = new Date("2026-01-01T00:00:00Z");

const WORKERS = {
  workers: [
    {
      id: "worker-a",
      version: "0.9.2",
      status: "online",
      assigned_count: 2,
      last_heartbeat_at: new Date(NOW.getTime() - 5_000).toISOString(),
      registered_at: NOW.toISOString(),
      capabilities: {
        drivers: ["container", "process"],
        max_servers: 8,
        resources: { cpu_cores: 4, memory_bytes: 3_221_225_472 },
      },
    },
    {
      id: "worker-b",
      version: "0.9.2",
      status: "draining",
      assigned_count: 1,
      last_heartbeat_at: new Date(NOW.getTime() - 120_000).toISOString(),
      registered_at: NOW.toISOString(),
      capabilities: {
        drivers: ["container"],
        max_servers: 4,
        resources: { cpu_cores: 2, memory_bytes: 1_073_741_824 },
      },
    },
  ],
};

function methodOf(init: RequestInit | undefined): string {
  return (init?.method ?? "GET").toUpperCase();
}

function signedIn(
  onDrain?: (url: string, init: RequestInit | undefined) => Response,
) {
  fetchMock.mockImplementation(
    (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/users/me") return Promise.resolve(jsonResponse(ADMIN));
      if (url === "/api/communities")
        return Promise.resolve(jsonResponse([{ id: "c1", name: "Alpha" }]));
      if (url.endsWith("/me/permissions"))
        return Promise.resolve(jsonResponse({}));
      if (url === "/api/workers" && methodOf(init) === "GET")
        return Promise.resolve(jsonResponse(WORKERS));
      if (/^\/api\/workers\/[^/]+\/drain$/.test(url) && onDrain !== undefined)
        return Promise.resolve(onDrain(url, init));
      return Promise.resolve(tokenResponse());
    },
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  clearAccessToken();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("admin workers page", () => {
  it("renders the fleet with humanized bytes and heartbeat age", async () => {
    // Advance fake timers so queries and async assertions progress from the reference timestamp.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW);

    signedIn();

    renderApp({ path: "/admin/workers" });

    expect(await screen.findByText("worker-a")).toBeInTheDocument();
    expect(screen.getByText("worker-b")).toBeInTheDocument();
    expect(screen.getByText("online")).toBeInTheDocument();
    expect(screen.getByText("draining")).toBeInTheDocument();

    expect(screen.getByText("process")).toBeInTheDocument();

    expect(
      screen.getByText(`4${t("admin.workers.cpuCores")} · 3.0 GiB`),
    ).toBeInTheDocument();

    expect(screen.getByText("5s ago")).toBeInTheDocument();
    expect(screen.getByText("2m ago")).toBeInTheDocument();
  });

  it("drains an online worker after confirm with PUT /workers/{id}/drain", async () => {
    const calls: { url: string; method: string }[] = [];
    signedIn((url, init) => {
      calls.push({ url, method: methodOf(init) });
      return jsonResponse({ servers_stopped: 0 });
    });

    renderApp({ path: "/admin/workers" });

    const drainButton = await screen.findByRole("button", {
      name: t("admin.workers.drain"),
    });
    fireEvent.click(drainButton);

    const confirm = await screen.findByRole("button", {
      name: t("admin.workers.drainConfirm"),
    });
    fireEvent.click(confirm);

    await waitFor(() => {
      expect(calls).toContainEqual({
        url: "/api/workers/worker-a/drain",
        method: "PUT",
      });
    });
  });

  it("shows convergence warning in the drain confirm dialog", async () => {
    signedIn();

    renderApp({ path: "/admin/workers" });

    fireEvent.click(
      await screen.findByRole("button", { name: t("admin.workers.drain") }),
    );

    expect(
      await screen.findByText(t("admin.workers.drainDialogConvergenceWarning")),
    ).toBeInTheDocument();
  });

  it("surfaces servers_stopped count in the success toast after drain", async () => {
    signedIn((_url, init) => {
      if ((init?.method ?? "GET").toUpperCase() === "PUT") {
        return jsonResponse({ servers_stopped: 3 });
      }
      return new Response(null, { status: 204 });
    });

    renderApp({ path: "/admin/workers" });

    fireEvent.click(
      await screen.findByRole("button", { name: t("admin.workers.drain") }),
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: t("admin.workers.drainConfirm"),
      }),
    );

    expect(
      await screen.findByText(t("admin.workers.drainedCount", { count: 3 })),
    ).toBeInTheDocument();
  });

  it("undrains a draining worker after confirm with DELETE /workers/{id}/drain", async () => {
    const calls: { url: string; method: string }[] = [];
    signedIn((url, init) => {
      calls.push({ url, method: methodOf(init) });
      return new Response(null, { status: 204 });
    });

    renderApp({ path: "/admin/workers" });

    const undrainButton = await screen.findByRole("button", {
      name: t("admin.workers.undrain"),
    });
    fireEvent.click(undrainButton);

    const confirm = await screen.findByRole("button", {
      name: t("admin.workers.undrainConfirm"),
    });
    fireEvent.click(confirm);

    await waitFor(() => {
      expect(calls).toContainEqual({
        url: "/api/workers/worker-b/drain",
        method: "DELETE",
      });
    });
  });

  it("cancel dismisses the confirm without calling the endpoint", async () => {
    const calls: string[] = [];
    signedIn((url) => {
      calls.push(url);
      return new Response(null, { status: 204 });
    });

    renderApp({ path: "/admin/workers" });

    fireEvent.click(
      await screen.findByRole("button", { name: t("admin.workers.drain") }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: t("common.cancel") }),
    );

    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: t("admin.workers.drainConfirm") }),
      ).not.toBeInTheDocument();
    });
    expect(calls).toHaveLength(0);
  });

  it("surfaces a toast error when drain fails", async () => {
    signedIn((_url) => new Response("nope", { status: 500 }));

    renderApp({ path: "/admin/workers" });

    fireEvent.click(
      await screen.findByRole("button", { name: t("admin.workers.drain") }),
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: t("admin.workers.drainConfirm"),
      }),
    );

    expect(
      await screen.findByText(t("admin.workers.drainError")),
    ).toBeInTheDocument();
  });

  it("shows the error state when the fleet list fails", async () => {
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

    renderApp({ path: "/admin/workers" });

    expect(
      await screen.findByText(t("admin.workers.loadError")),
    ).toBeInTheDocument();
  });
});
