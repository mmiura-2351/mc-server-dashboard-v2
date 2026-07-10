// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { describe, expect, it } from "vitest";
import { ApiError } from "./client.ts";
import { labelQueryFn } from "./labelQuery.ts";

describe("labelQueryFn", () => {
  it("passes through the resolved value when the read succeeds", async () => {
    const fn = labelQueryFn(() => Promise.resolve([{ id: "a" }]), []);
    await expect(fn()).resolves.toEqual([{ id: "a" }]);
  });

  it("swallows a 403 into the supplied empty value", async () => {
    const fn = labelQueryFn(
      () => Promise.reject(new ApiError(403, { reason: "forbidden" })),
      [],
    );
    await expect(fn()).resolves.toEqual([]);
  });

  it("rethrows a non-403 ApiError (404)", async () => {
    const err = new ApiError(404, { reason: "not_found" });
    const fn = labelQueryFn(() => Promise.reject(err), []);
    await expect(fn()).rejects.toBe(err);
  });

  it("rethrows a non-403 ApiError (500)", async () => {
    const err = new ApiError(500, { reason: "server_error" });
    const fn = labelQueryFn(() => Promise.reject(err), []);
    await expect(fn()).rejects.toBe(err);
  });

  it("rethrows a non-ApiError (network) error", async () => {
    const err = new TypeError("network down");
    const fn = labelQueryFn(() => Promise.reject(err), []);
    await expect(fn()).rejects.toBe(err);
  });

  it("forwards the query-function context to the wrapped fn", async () => {
    // TanStack calls the queryFn with a context carrying the abort signal;
    // the wrapper must pass it through so requests stay cancellable (#1728).
    const seen: unknown[] = [];
    const fn = labelQueryFn((context: { signal: AbortSignal }) => {
      seen.push(context);
      return Promise.resolve([]);
    }, []);
    const context = { signal: new AbortController().signal };

    await fn(context);

    expect(seen).toEqual([context]);
  });
});
