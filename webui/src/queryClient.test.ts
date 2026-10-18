// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file DOM setup.
import { describe, expect, it } from "vitest";
import { ApiError } from "./api/client.ts";
import { createQueryClient, shouldRetryQuery } from "./queryClient.ts";

describe("shouldRetryQuery", () => {
  it.each([400, 401, 403, 404, 409, 413, 422])(
    "does not retry a deterministic %i",
    (status) => {
      expect(shouldRetryQuery(0, new ApiError(status, {}))).toBe(false);
    },
  );

  it.each([
    ["a 408", new ApiError(408, {})],
    ["a 429", new ApiError(429, {})],
    ["a 500", new ApiError(500, {})],
    ["a 503", new ApiError(503, { reason: "worker_unavailable" })],
    ["a network failure", new TypeError("Failed to fetch")],
  ])("retries %s", (_name, error) => {
    expect(shouldRetryQuery(0, error)).toBe(true);
    expect(shouldRetryQuery(2, error)).toBe(true);
  });

  it("stops after three retries", () => {
    expect(shouldRetryQuery(3, new ApiError(503, {}))).toBe(false);
  });
});

describe("createQueryClient", () => {
  it("applies the retry policy to every query by default", () => {
    expect(createQueryClient().getDefaultOptions().queries?.retry).toBe(
      shouldRetryQuery,
    );
  });
});
