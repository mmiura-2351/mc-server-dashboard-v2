// @vitest-environment node
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

  it("maps a 409 server_unsettled to the at-rest precondition message", () => {
    const error = new ApiError(409, { reason: "server_unsettled" });
    expect(lifecycleErrorMessage(error)).toBe("serverDetail.error.unsettled");
  });

  it.each(["worker_busy", "server_busy"])(
    "maps a 409 %s to the busy message",
    (reason) => {
      const error = new ApiError(409, { reason });
      expect(lifecycleErrorMessage(error)).toBe("dashboard.lifecycle.busy");
    },
  );

  it("maps a 409 command_failed to the dispatch-failure message", () => {
    const error = new ApiError(409, { reason: "command_failed" });
    expect(lifecycleErrorMessage(error)).toBe(
      "dashboard.lifecycle.commandFailed",
    );
  });

  it("keeps the dispatch-failure message for a failed start", () => {
    const error = new ApiError(409, { reason: "command_failed" });
    expect(lifecycleErrorMessage(error, "start")).toBe(
      "dashboard.lifecycle.commandFailed",
    );
  });

  it.each(["command_failed", "worker_busy"])(
    "maps a 409 %s on stop to the pending-stop message",
    (reason) => {
      const error = new ApiError(409, { reason });
      expect(lifecycleErrorMessage(error, "stop")).toBe(
        "dashboard.lifecycle.stopPending",
      );
    },
  );

  it("maps a 409 command_failed on restart to the pending-restart message", () => {
    const error = new ApiError(409, { reason: "command_failed" });
    expect(lifecycleErrorMessage(error, "restart")).toBe(
      "dashboard.lifecycle.restartPending",
    );
  });

  it("maps a 409 server_not_running on restart to the pending-restart message", () => {
    const error = new ApiError(409, { reason: "server_not_running" });
    expect(lifecycleErrorMessage(error, "restart")).toBe(
      "dashboard.lifecycle.restartPending",
    );
  });

  // A failed-stop orphan may still be running; the host already retries its stop, and restart is not pending.
  it.each([undefined, "restart"] as const)(
    "maps a 409 failed_stop_orphan on %s to the orphan message",
    (action) => {
      const error = new ApiError(409, { reason: "failed_stop_orphan" });
      expect(lifecycleErrorMessage(error, action)).toBe(
        "dashboard.lifecycle.failedStopOrphan",
      );
    },
  );

  it("keeps the busy message for a 409 worker_busy on restart", () => {
    const error = new ApiError(409, { reason: "worker_busy" });
    expect(lifecycleErrorMessage(error, "restart")).toBe(
      "dashboard.lifecycle.busy",
    );
  });

  // worker_busy before dispatch compensates start; after dispatch it can remain pending, so promise neither outcome.
  it("maps a 409 worker_busy on start to the start-pending message", () => {
    const error = new ApiError(409, { reason: "worker_busy" });
    expect(lifecycleErrorMessage(error, "start")).toBe(
      "dashboard.lifecycle.startPending",
    );
  });

  it("keeps the busy message for a 409 server_busy on start", () => {
    const error = new ApiError(409, { reason: "server_busy" });
    expect(lifecycleErrorMessage(error, "start")).toBe(
      "dashboard.lifecycle.busy",
    );
  });

  it.each(["invalid_transition", "transition_conflict", "server_not_running"])(
    "keeps the state-changed treatment for an unmapped 409 reason (%s)",
    (reason) => {
      const error = new ApiError(409, { reason });
      expect(lifecycleErrorMessage(error)).toBe("dashboard.stateChanged");
    },
  );

  it("maps a 409 invalid_transition on start to the already-running message", () => {
    const error = new ApiError(409, { reason: "invalid_transition" });
    expect(lifecycleErrorMessage(error, "start")).toBe(
      "dashboard.lifecycle.startAlreadyRunning",
    );
  });

  it.each(["stop", "restart"] as const)(
    "keeps the state-changed treatment for a 409 invalid_transition on %s",
    (action) => {
      const error = new ApiError(409, { reason: "invalid_transition" });
      expect(lifecycleErrorMessage(error, action)).toBe(
        "dashboard.stateChanged",
      );
    },
  );

  it("treats a 409 with no reason as state-changed", () => {
    const error = new ApiError(409, undefined);
    expect(lifecycleErrorMessage(error)).toBe("dashboard.stateChanged");
  });

  it("falls back to the generic message for non-409 errors", () => {
    const error = new ApiError(500, { reason: "internal_error" });
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

  // A stop timeout leaves its committed intent pending, but does not confirm whether the server has stopped.
  it("maps a 503 worker_unavailable on stop to the unconfirmed-stop message", () => {
    const error = new ApiError(503, { reason: "worker_unavailable" });
    expect(lifecycleErrorMessage(error, "stop")).toBe(
      "dashboard.lifecycle.stopUnconfirmed",
    );
  });

  it("maps a 503 worker_unavailable on restart to the unconfirmed-restart message", () => {
    const error = new ApiError(503, { reason: "worker_unavailable" });
    expect(lifecycleErrorMessage(error, "restart")).toBe(
      "dashboard.lifecycle.restartUnconfirmed",
    );
  });

  // worker_unavailable can precede or follow start dispatch, so the client cannot promise a pending start.
  it("keeps the worker-unavailable message for a failed start", () => {
    const error = new ApiError(503, { reason: "worker_unavailable" });
    expect(lifecycleErrorMessage(error, "start")).toBe(
      "dashboard.lifecycle.workerUnavailable",
    );
  });

  it("keeps the no-eligible-worker message on stop", () => {
    const error = new ApiError(503, { reason: "no_eligible_worker" });
    expect(lifecycleErrorMessage(error, "stop")).toBe(
      "dashboard.lifecycle.noEligibleWorker",
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
