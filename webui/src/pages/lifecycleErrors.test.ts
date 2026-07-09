// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client.ts";
import { lifecycleErrorMessage } from "./lifecycleErrors.ts";

describe("lifecycleErrorMessage", () => {
  it("maps a 409 port_conflict to its specific message", () => {
    const error = new ApiError(409, { reason: "port_conflict" });
    expect(lifecycleErrorMessage(error)).toBe(
      "dashboard.lifecycle.portConflict",
    );
  });

  it("maps a 409 image_missing to its specific message", () => {
    const error = new ApiError(409, { reason: "image_missing" });
    expect(lifecycleErrorMessage(error)).toBe(
      "dashboard.lifecycle.imageMissing",
    );
  });

  it.each([
    "server_unsettled",
    "invalid_transition",
    "transition_conflict",
    "command_failed",
    "server_not_running",
  ])("gives an unknown 409 reason (%s) the state-changed treatment", (reason) => {
    const error = new ApiError(409, { reason });
    expect(lifecycleErrorMessage(error)).toBe("dashboard.stateChanged");
  });

  it("treats a 409 with no reason as state-changed", () => {
    const error = new ApiError(409, undefined);
    expect(lifecycleErrorMessage(error)).toBe("dashboard.stateChanged");
  });

  it("falls back to the generic message for non-409 errors", () => {
    const error = new ApiError(500, { reason: "server_error" });
    expect(lifecycleErrorMessage(error)).toBe("dashboard.actionFailed");
  });

  it("falls back to the generic message for a non-ApiError", () => {
    expect(lifecycleErrorMessage(new Error("boom"))).toBe(
      "dashboard.actionFailed",
    );
  });

  it("does not treat a port_conflict reason on a non-409 status as specific", () => {
    const error = new ApiError(503, { reason: "port_conflict" });
    expect(lifecycleErrorMessage(error)).toBe("dashboard.actionFailed");
  });

  // 503 service-unavailable reasons (issue #1092).
  it("maps a 503 no_eligible_worker to its specific message", () => {
    const error = new ApiError(503, { reason: "no_eligible_worker" });
    expect(lifecycleErrorMessage(error)).toBe(
      "dashboard.lifecycle.noEligibleWorker",
    );
  });

  it("maps a 503 worker_unavailable to its specific message", () => {
    const error = new ApiError(503, { reason: "worker_unavailable" });
    expect(lifecycleErrorMessage(error)).toBe(
      "dashboard.lifecycle.workerUnavailable",
    );
  });

  it("maps a 503 jar_unavailable to its specific message", () => {
    const error = new ApiError(503, { reason: "jar_unavailable" });
    expect(lifecycleErrorMessage(error)).toBe(
      "dashboard.lifecycle.jarUnavailable",
    );
  });

  it("falls back to the generic message for a 503 with an unknown reason", () => {
    const error = new ApiError(503, { reason: "something_else" });
    expect(lifecycleErrorMessage(error)).toBe("dashboard.actionFailed");
  });

  it("falls back to the generic message for a 503 with no reason", () => {
    const error = new ApiError(503, undefined);
    expect(lifecycleErrorMessage(error)).toBe("dashboard.actionFailed");
  });
});
