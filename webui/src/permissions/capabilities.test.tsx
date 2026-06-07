import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import { setAccessToken } from "../auth/tokenStore.ts";
import { ToastProvider } from "../components/Toast.tsx";
import {
  ActiveCommunityProvider,
  useActiveCommunity,
} from "./ActiveCommunityProvider.tsx";
import { useCan } from "./useCan.ts";
import { useOnForbidden } from "./useOnForbidden.ts";

// The session core is stubbed: these tests exercise the capability layer, not
// the bootstrap. A signed-in session is the precondition for fetching.
vi.mock("../auth/SessionProvider.tsx", () => ({
  useSession: () => ({ status: "signed-in", logout: vi.fn() }),
}));

const fetchMock = vi.fn();

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Match a request URL (fetch is called with a path string). */
function urlOf(call: unknown[]): string {
  return String(call[0]);
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  setAccessToken("test-token");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function wrap(ui: ReactNode, client: QueryClient) {
  return (
    <QueryClientProvider client={client}>
      <ToastProvider>
        <ActiveCommunityProvider>{ui}</ActiveCommunityProvider>
      </ToastProvider>
    </QueryClientProvider>
  );
}

function newClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
}

function CanProbe() {
  const { communityId } = useActiveCommunity();
  const can = useCan();
  return (
    <div>
      <span data-testid="cid">{communityId ?? "none"}</span>
      <span data-testid="start">{String(can("server:start"))}</span>
      <span data-testid="start-s1">
        {String(can("server:start", { serverId: "s1" }))}
      </span>
    </div>
  );
}

describe("default-community selection", () => {
  it("defaults the active community to the first from GET /communities", async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url === "/api/communities") {
        return Promise.resolve(
          jsonResponse([
            { id: "c1", name: "First" },
            { id: "c2", name: "Second" },
          ]),
        );
      }
      return Promise.resolve(jsonResponse({ permissions: [], grants: [] }));
    });

    render(wrap(<CanProbe />, newClient()));

    await waitFor(() =>
      expect(screen.getByTestId("cid")).toHaveTextContent("c1"),
    );
  });

  it("leaves the active community null when the user has none", async () => {
    fetchMock.mockResolvedValue(jsonResponse([]));

    render(wrap(<CanProbe />, newClient()));

    await waitFor(() =>
      expect(screen.getByTestId("cid")).toHaveTextContent("none"),
    );
    // No community means no permissions fetch.
    expect(
      fetchMock.mock.calls.some((c) => urlOf(c).includes("/me/permissions")),
    ).toBe(false);
  });
});

describe("can() resolution against the fetched set", () => {
  it("resolves community codes and matching resource grants for the active community", async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url === "/api/communities") {
        return Promise.resolve(jsonResponse([{ id: "c1", name: "First" }]));
      }
      return Promise.resolve(
        jsonResponse({
          permissions: [],
          grants: [
            {
              resource_type: "server",
              resource_id: "s1",
              permissions: ["server:start"],
            },
          ],
        }),
      );
    });

    render(wrap(<CanProbe />, newClient()));

    await waitFor(() =>
      expect(screen.getByTestId("start-s1")).toHaveTextContent("true"),
    );
    // Community-wide check is denied: the grant is server-scoped.
    expect(screen.getByTestId("start")).toHaveTextContent("false");
  });
});

describe("per-community cache isolation", () => {
  function Switcher() {
    const { communityId, setCommunityId } = useActiveCommunity();
    const can = useCan();
    return (
      <div>
        <span data-testid="cid">{communityId ?? "none"}</span>
        <span data-testid="start">{String(can("server:start"))}</span>
        <button type="button" onClick={() => setCommunityId("c2")}>
          to-c2
        </button>
      </div>
    );
  }

  it("fetches and caches each community's set separately", async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url === "/api/communities") {
        return Promise.resolve(
          jsonResponse([
            { id: "c1", name: "First" },
            { id: "c2", name: "Second" },
          ]),
        );
      }
      if (url.includes("/api/communities/c1/")) {
        return Promise.resolve(
          jsonResponse({ permissions: ["server:start"], grants: [] }),
        );
      }
      // c2 lacks the code.
      return Promise.resolve(jsonResponse({ permissions: [], grants: [] }));
    });

    render(wrap(<Switcher />, newClient()));

    await waitFor(() =>
      expect(screen.getByTestId("start")).toHaveTextContent("true"),
    );

    act(() => {
      screen.getByRole("button", { name: "to-c2" }).click();
    });

    await waitFor(() =>
      expect(screen.getByTestId("cid")).toHaveTextContent("c2"),
    );
    await waitFor(() =>
      expect(screen.getByTestId("start")).toHaveTextContent("false"),
    );

    const permCalls = fetchMock.mock.calls.filter((c) =>
      urlOf(c).includes("/me/permissions"),
    );
    expect(permCalls.map(urlOf)).toEqual([
      "/api/communities/c1/me/permissions",
      "/api/communities/c2/me/permissions",
    ]);
  });
});

describe("re-fetch on 403", () => {
  function ForbiddenProbe() {
    const onForbidden = useOnForbidden();
    const { communityId } = useActiveCommunity();
    const can = useCan();
    return (
      <div>
        <span data-testid="cid">{communityId ?? "none"}</span>
        <span data-testid="start">{String(can("server:start"))}</span>
        <button
          type="button"
          onClick={() =>
            onForbidden(new ApiError(403, { reason: "server:start" }))
          }
        >
          forbid
        </button>
      </div>
    );
  }

  it("re-fetches the active community's capabilities after a 403", async () => {
    let permCallCount = 0;
    fetchMock.mockImplementation((url: string) => {
      if (url === "/api/communities") {
        return Promise.resolve(jsonResponse([{ id: "c1", name: "First" }]));
      }
      permCallCount += 1;
      // First fetch lacks the code; after the 403 re-fetch it is granted.
      return Promise.resolve(
        jsonResponse(
          permCallCount === 1
            ? { permissions: [], grants: [] }
            : { permissions: ["server:start"], grants: [] },
        ),
      );
    });

    render(wrap(<ForbiddenProbe />, newClient()));

    await waitFor(() =>
      expect(screen.getByTestId("cid")).toHaveTextContent("c1"),
    );
    await waitFor(() =>
      expect(screen.getByTestId("start")).toHaveTextContent("false"),
    );
    expect(permCallCount).toBe(1);

    act(() => {
      screen.getByRole("button", { name: "forbid" }).click();
    });

    await waitFor(() => expect(permCallCount).toBeGreaterThanOrEqual(2));
    await waitFor(() =>
      expect(screen.getByTestId("start")).toHaveTextContent("true"),
    );
  });
});
