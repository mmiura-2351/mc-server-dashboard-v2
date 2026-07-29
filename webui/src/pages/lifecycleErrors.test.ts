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

  it("maps a 409 server_unsettled to the at-rest precondition message", () => {
    const error = new ApiError(409, { reason: "server_unsettled" });
    expect(lifecycleErrorMessage(error)).toBe("serverDetail.error.unsettled");
  });

  // Contention, not a race: the request was refused without being applied and
  // clears on its own (issue #2400).
  it.each(["worker_busy", "server_busy"])(
    "maps a 409 %s to the busy message",
    (reason) => {
      const error = new ApiError(409, { reason });
      expect(lifecycleErrorMessage(error)).toBe("dashboard.lifecycle.busy");
    },
  );

  // The dispatch reached the Worker and was not applied as asked; whether the
  // server moved depends on the verb, so the toast names the failure rather
  // than claiming a state change (issue #2420).
  it("maps a 409 command_failed to the dispatch-failure message", () => {
    const error = new ApiError(409, { reason: "command_failed" });
    expect(lifecycleErrorMessage(error)).toBe(
      "dashboard.lifecycle.commandFailed",
    );
  });

  // A failed start is compensated back to stopped, so nothing is pending and
  // the verb-agnostic message is already honest (issue #2435).
  it("keeps the dispatch-failure message for a failed start", () => {
    const error = new ApiError(409, { reason: "command_failed" });
    expect(lifecycleErrorMessage(error, "start")).toBe(
      "dashboard.lifecycle.commandFailed",
    );
  });

  // A failed stop leaves desired_state=stopped committed over a still-running
  // process, so the stop intent is pending and the reconciler retries it. Both
  // reasons the API can render for it say so (issue #2435).
  it.each(["command_failed", "worker_busy"])(
    "maps a 409 %s on stop to the pending-stop message",
    (reason) => {
      const error = new ApiError(409, { reason });
      expect(lifecycleErrorMessage(error, "stop")).toBe(
        "dashboard.lifecycle.stopPending",
      );
    },
  );

  // A failed restart leaves desired_state=running, so a server the Worker took
  // down is brought back automatically (issue #2435).
  it("maps a 409 command_failed on restart to the pending-restart message", () => {
    const error = new ApiError(409, { reason: "command_failed" });
    expect(lifecycleErrorMessage(error, "restart")).toBe(
      "dashboard.lifecycle.restartPending",
    );
  });

  // Restart is the one verb for which server_not_running is not a race: the
  // dashboard deliberately offers restart for a crashed server that is still
  // desired-running, so "state changed" names nothing that changed (issue
  // #2441). What is left pending is what a failed restart always leaves —
  // desired_state=running — so it shares that message.
  it("maps a 409 server_not_running on restart to the pending-restart message", () => {
    const error = new ApiError(409, { reason: "server_not_running" });
    expect(lifecycleErrorMessage(error, "restart")).toBe(
      "dashboard.lifecycle.restartPending",
    );
  });

  // worker_busy on restart applied nothing at all — the server keeps running —
  // so it stays on the retry-in-a-moment message (issue #2435).
  it.each(["start", "restart"] as const)(
    "keeps the busy message for a 409 worker_busy on %s",
    (action) => {
      const error = new ApiError(409, { reason: "worker_busy" });
      expect(lifecycleErrorMessage(error, action)).toBe(
        "dashboard.lifecycle.busy",
      );
    },
  );

  // server_busy is start-only and never commits an intent, so the verb never
  // changes its message (issue #2435).
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

  // A stop dispatch that timed out or lost its session failed AFTER the API
  // committed desired_state=stopped, so the stop intent stands and the
  // reconciler redispatches it — but the outcome is unknown, so the message
  // can only say it is unconfirmed (issue #2440).
  it("maps a 503 worker_unavailable on stop to the unconfirmed-stop message", () => {
    const error = new ApiError(503, { reason: "worker_unavailable" });
    expect(lifecycleErrorMessage(error, "stop")).toBe(
      "dashboard.lifecycle.stopUnconfirmed",
    );
  });

  // A restart dispatch that could not be confirmed leaves desired_state=running,
  // so a server the Worker took down and failed to relaunch comes back on its
  // own (issue #2440).
  it("maps a 503 worker_unavailable on restart to the unconfirmed-restart message", () => {
    const error = new ApiError(503, { reason: "worker_unavailable" });
    expect(lifecycleErrorMessage(error, "restart")).toBe(
      "dashboard.lifecycle.restartUnconfirmed",
    );
  });

  // Start is ambiguous: a pre-dispatch unavailable compensates back to stopped
  // (nothing pending), a post-dispatch one does not, and both arrive as a bare
  // worker_unavailable — so it keeps the verb-agnostic message (issue #2440).
  it("keeps the worker-unavailable message for a failed start", () => {
    const error = new ApiError(503, { reason: "worker_unavailable" });
    expect(lifecycleErrorMessage(error, "start")).toBe(
      "dashboard.lifecycle.workerUnavailable",
    );
  });

  // no_eligible_worker is raised before any intent is committed, on every verb
  // that can see it, so it never becomes verb-specific (issue #2440).
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
