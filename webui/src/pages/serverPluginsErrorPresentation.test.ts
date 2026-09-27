// @vitest-environment node
// DOM-free contract test: the Plugins tab renders the selected key with its noun.
import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client.ts";
import { pluginErrorPresentation } from "./serverPluginsErrorPresentation.ts";

describe("pluginErrorPresentation", () => {
  it.each([
    [
      "maps plugin_already_exists",
      new ApiError(409, { reason: "plugin_already_exists" }),
      "plugins.error.alreadyExists",
    ],
    [
      "maps server_not_stopped",
      new ApiError(409, { reason: "server_not_stopped" }),
      "plugins.error.notStopped",
    ],
    [
      "maps server_unsettled",
      new ApiError(409, { reason: "server_unsettled" }),
      "plugins.error.unsettled",
    ],
    [
      "maps server_busy",
      new ApiError(409, { reason: "server_busy" }),
      "plugins.error.busy",
    ],
    [
      "maps invalid_path",
      new ApiError(422, { reason: "invalid_path" }),
      "plugins.error.invalidPath",
    ],
    [
      "maps catalog_upstream_failed",
      new ApiError(502, { reason: "catalog_upstream_failed" }),
      "plugins.error.catalogUpstreamFailed",
    ],
    [
      "maps catalog_project_not_found",
      new ApiError(404, { reason: "catalog_project_not_found" }),
      "plugins.error.catalogNotFound",
    ],
    [
      "maps checksum_mismatch",
      new ApiError(502, { reason: "checksum_mismatch" }),
      "plugins.error.checksumMismatch",
    ],
    [
      "maps unsupported_server_type",
      new ApiError(422, { reason: "unsupported_server_type" }),
      "plugins.error.unsupportedServerType",
    ],
    [
      "maps invalid_side",
      new ApiError(422, { reason: "invalid_side" }),
      "plugins.error.invalidSide",
    ],
    [
      "maps invalid_display_name",
      new ApiError(422, { reason: "invalid_display_name" }),
      "plugins.error.invalidDisplayName",
    ],
    [
      "maps bedrock_port_range_exhausted",
      new ApiError(503, { reason: "bedrock_port_range_exhausted" }),
      "plugins.error.bedrockPortRangeExhausted",
    ],
    [
      "maps bedrock_port_taken",
      new ApiError(409, { reason: "bedrock_port_taken" }),
      "plugins.error.bedrockPortTaken",
    ],
    [
      "maps not_found",
      new ApiError(404, { reason: "not_found" }),
      "plugins.error.notFound",
    ],
    [
      "maps file_too_large by status",
      new ApiError(413, { reason: "file_too_large" }),
      "plugins.error.tooLarge",
    ],
    [
      "maps a reason-less 503 by status",
      new ApiError(503, {}),
      "plugins.error.workerUnavailable",
    ],
    [
      "falls back for an unknown reason",
      new ApiError(409, { reason: "unknown_reason" }),
      "plugins.error.generic",
    ],
    [
      "falls back for a non-API error",
      new Error("boom"),
      "plugins.error.generic",
    ],
  ])("%s", (_name, error, expected) => {
    expect(pluginErrorPresentation(error)).toBe(expected);
  });
});
