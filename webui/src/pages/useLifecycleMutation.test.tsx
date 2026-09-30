import {
  QueryClient,
  QueryClientProvider,
  type QueryKey,
} from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client.ts";
import type { components } from "../api/schema";
import type { LifecycleAction } from "./lifecycleErrors.ts";
import { serverKey } from "./serverKey.ts";
import { serversKey } from "./useCommunityEvents.ts";
import { useLifecycleMutation } from "./useLifecycleMutation.ts";

type ServerResponse = components["schemas"]["ServerResponse"];

const CID = "c1";

const mockApi = vi.hoisted(() => ({ post: vi.fn() }));

vi.mock("../api/client.ts", async () => {
  const actual =
    await vi.importActual<typeof import("../api/client.ts")>(
      "../api/client.ts",
    );
  return { ...actual, api: mockApi };
});

function server(id: string, observed_state: string): ServerResponse {
  return { id, observed_state } as ServerResponse;
}

// A POST the test settles by hand, so a status update can land between the
// optimistic write and the API's answer.
function deferred() {
  let resolve!: (value: unknown) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

let queryClient: QueryClient;

beforeEach(() => {
  mockApi.post.mockReset();
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
});

function renderLifecycle(
  serverId: string,
  cacheKey: QueryKey,
  invalidateKeys: readonly QueryKey[] = [cacheKey],
) {
  const onError = vi.fn();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(
    () =>
      useLifecycleMutation({
        communityId: CID,
        serverId,
        cacheKey,
        invalidateKeys,
        onError,
      }),
    { wrapper },
  );
  return { result, onError };
}

// The two cache shapes the surfaces keep a server's state in: one element of
// the dashboard's community list, or the detail page's single server. `seed`
// writes s1's state, `read` returns it, and `push` is the WS status patch the
// matching events hook applies (useCommunityEvents / useServerEvents).
const CACHES = [
  {
    shape: "community list",
    key: serversKey(CID),
    seed: (state: string) =>
      queryClient.setQueryData<ServerResponse[]>(serversKey(CID), [
        server("s1", state),
        server("s2", "running"),
      ]),
    read: () =>
      queryClient
        .getQueryData<ServerResponse[]>(serversKey(CID))
        ?.find((s) => s.id === "s1")?.observed_state,
    push: (state: string) =>
      queryClient.setQueryData<ServerResponse[]>(serversKey(CID), (old) =>
        old?.map((s) => (s.id === "s1" ? { ...s, observed_state: state } : s)),
      ),
  },
  {
    shape: "server detail",
    key: serverKey(CID, "s1"),
    seed: (state: string) =>
      queryClient.setQueryData(serverKey(CID, "s1"), server("s1", state)),
    read: () =>
      queryClient.getQueryData<ServerResponse>(serverKey(CID, "s1"))
        ?.observed_state,
    push: (state: string) =>
      queryClient.setQueryData<ServerResponse>(serverKey(CID, "s1"), (old) =>
        old ? { ...old, observed_state: state } : old,
      ),
  },
];

function listState(id: string) {
  return queryClient
    .getQueryData<ServerResponse[]>(serversKey(CID))
    ?.find((s) => s.id === id)?.observed_state;
}

describe("useLifecycleMutation", () => {
  it.each<{ action: LifecycleAction; from: string; transitional: string }>([
    { action: "start", from: "stopped", transitional: "starting" },
    { action: "stop", from: "running", transitional: "stopping" },
    { action: "restart", from: "running", transitional: "restarting" },
  ])(
    "posts $action and shows $transitional while it is in flight",
    async ({ action, from, transitional }) => {
      const [cache] = CACHES;
      cache.seed(from);
      mockApi.post.mockReturnValue(new Promise(() => {}));
      const { result } = renderLifecycle("s1", cache.key);

      act(() => result.current.mutate({ action }));

      await waitFor(() => expect(cache.read()).toBe(transitional));
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/s1/${action}`,
      );
      expect(result.current.isPending).toBe(true);
    },
  );

  it("appends the request's query string to the verb path", async () => {
    const [cache] = CACHES;
    cache.seed("running");
    mockApi.post.mockReturnValue(new Promise(() => {}));
    const { result } = renderLifecycle("s1", cache.key);

    act(() => result.current.mutate({ action: "stop", query: "force=true" }));

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        `/api/communities/${CID}/servers/s1/stop?force=true`,
      ),
    );
    expect(cache.read()).toBe("stopping");
  });

  it("keeps the transitional state on success and invalidates every key", async () => {
    const detail = serverKey(CID, "s1");
    queryClient.setQueryData(detail, server("s1", "stopped"));
    queryClient.setQueryData(serversKey(CID), [server("s1", "stopped")]);
    const post = deferred();
    mockApi.post.mockReturnValue(post.promise);
    const { result, onError } = renderLifecycle("s1", detail, [
      detail,
      serversKey(CID),
    ]);

    act(() => result.current.mutate({ action: "start" }));
    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    expect(queryClient.getQueryState(detail)?.isInvalidated).toBe(false);

    await act(async () => post.resolve(undefined));

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(
      queryClient.getQueryData<ServerResponse>(detail)?.observed_state,
    ).toBe("starting");
    expect(queryClient.getQueryState(detail)?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(serversKey(CID))?.isInvalidated).toBe(
      true,
    );
    expect(onError).not.toHaveBeenCalled();
  });

  describe.each(CACHES)("in the $shape cache", (cache) => {
    it("rolls back to the captured state when nothing newer arrived", async () => {
      cache.seed("running");
      const post = deferred();
      mockApi.post.mockReturnValue(post.promise);
      const { result, onError } = renderLifecycle("s1", cache.key);

      act(() => result.current.mutate({ action: "restart" }));
      await waitFor(() => expect(cache.read()).toBe("restarting"));

      const error = new ApiError(409, { reason: "worker_busy" });
      await act(async () => post.reject(error));

      await waitFor(() =>
        expect(onError).toHaveBeenCalledWith(error, "restart"),
      );
      expect(cache.read()).toBe("running");
      // Failure refetches too: the server's truth replaces the rollback.
      expect(queryClient.getQueryState(cache.key)?.isInvalidated).toBe(true);
    });

    it("keeps a status update that landed mid-flight when the request then fails (#1727)", async () => {
      cache.seed("stopped");
      const post = deferred();
      mockApi.post.mockReturnValue(post.promise);
      const { result, onError } = renderLifecycle("s1", cache.key);

      act(() => result.current.mutate({ action: "start" }));
      await waitFor(() => expect(cache.read()).toBe("starting"));

      // A WS status frame reports the server running while the POST is open.
      act(() => cache.push("running"));
      const error = new ApiError(503, { reason: "worker_unavailable" });
      await act(async () => post.reject(error));

      await waitFor(() => expect(onError).toHaveBeenCalledWith(error, "start"));
      // The captured "stopped" is older than the frame and must not win.
      expect(cache.read()).toBe("running");
    });
  });

  it("rolls back only its own server, leaving a mid-flight update to another intact", async () => {
    CACHES[0].seed("stopped");
    const post = deferred();
    mockApi.post.mockReturnValue(post.promise);
    const { result } = renderLifecycle("s1", serversKey(CID));

    act(() => result.current.mutate({ action: "start" }));
    await waitFor(() => expect(listState("s1")).toBe("starting"));

    act(() => {
      queryClient.setQueryData<ServerResponse[]>(serversKey(CID), (old) =>
        old?.map((s) =>
          s.id === "s2" ? { ...s, observed_state: "crashed" } : s,
        ),
      );
    });
    await act(async () => post.reject(new ApiError(500, {})));

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(listState("s1")).toBe("stopped");
    expect(listState("s2")).toBe("crashed");
  });

  it("keeps concurrent mutations on different servers independent", async () => {
    queryClient.setQueryData(serversKey(CID), [
      server("s1", "stopped"),
      server("s2", "running"),
    ]);
    const startS1 = deferred();
    const stopS2 = deferred();
    mockApi.post.mockImplementation((path: string) =>
      path.includes("/s1/") ? startS1.promise : stopS2.promise,
    );
    const s1 = renderLifecycle("s1", serversKey(CID));
    const s2 = renderLifecycle("s2", serversKey(CID));

    act(() => s1.result.current.mutate({ action: "start" }));
    act(() => s2.result.current.mutate({ action: "stop" }));
    await waitFor(() => expect(listState("s1")).toBe("starting"));
    await waitFor(() => expect(listState("s2")).toBe("stopping"));

    await act(async () => startS1.reject(new ApiError(500, {})));

    await waitFor(() => expect(s1.result.current.isError).toBe(true));
    expect(listState("s1")).toBe("stopped");
    expect(listState("s2")).toBe("stopping");
    expect(s2.result.current.isPending).toBe(true);
    expect(s2.onError).not.toHaveBeenCalled();

    await act(async () => stopS2.resolve(undefined));

    await waitFor(() => expect(s2.result.current.isSuccess).toBe(true));
    expect(listState("s1")).toBe("stopped");
    expect(listState("s2")).toBe("stopping");
  });
});
