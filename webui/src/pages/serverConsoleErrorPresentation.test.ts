// @vitest-environment node
// DOM-free contract test: the Console tab appends the selected key as a
// transcript line under the failed command (issue #2450).
import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client.ts";
import { consoleCommandErrorPresentation } from "./serverConsoleErrorPresentation.ts";

describe("consoleCommandErrorPresentation", () => {
  it.each([
    [
      "names an unreachable host, whose command may or may not have run",
      new ApiError(503, { reason: "worker_unavailable" }),
      "serverDetail.console.error.workerUnavailable",
    ],
    [
      "names a server that is not running",
      new ApiError(409, { reason: "server_not_running" }),
      "serverDetail.console.error.notRunning",
    ],
    [
      "names a failed console (RCON) connection to a running server",
      new ApiError(409, { reason: "command_failed" }),
      "serverDetail.console.error.rconFailed",
    ],
    [
      "names a server whose previous stop did not finish",
      new ApiError(409, { reason: "failed_stop_orphan" }),
      "serverDetail.console.error.failedStopOrphan",
    ],
  ])("%s", (_name, error, key) => {
    expect(consoleCommandErrorPresentation(error)).toBe(key);
  });

  it.each([
    ["an unenumerated 409 reason", new ApiError(409, { reason: "other" })],
    ["a 503 without a reason", new ApiError(503, {})],
    ["a 404", new ApiError(404, { reason: "not_found" })],
    ["a non-API error", new Error("network down")],
  ])("falls back to the generic line for %s", (_name, error) => {
    expect(consoleCommandErrorPresentation(error)).toBe(
      "serverDetail.commandFailed",
    );
  });
});
