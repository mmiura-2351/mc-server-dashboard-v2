// @vitest-environment node
// DOM-free contract test: the Files tab renders the selected presentation.
import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client.ts";
import { UnencodableTextError } from "./fileText.ts";
import { fileOperationErrorPresentation } from "./serverFilesErrorPresentation.ts";

describe("fileOperationErrorPresentation", () => {
  it.each([
    [
      "maps a local encoding refusal",
      new UnencodableTextError(),
      { key: "files.error.unencodableText" },
    ],
    [
      "maps a missing file by status",
      new ApiError(404, { reason: "not_found" }),
      { key: "files.error.notFound" },
    ],
    [
      "maps server_unsettled to the at-rest message",
      new ApiError(409, { reason: "server_unsettled" }),
      { key: "files.error.serverMustBeStopped" },
    ],
    [
      "maps server_not_stopped to the at-rest message",
      new ApiError(409, { reason: "server_not_stopped" }),
      { key: "files.error.serverMustBeStopped" },
    ],
    [
      "maps server_busy to the contention message",
      new ApiError(409, { reason: "server_busy" }),
      { key: "files.error.serverBusy" },
    ],
    [
      "maps an otherwise unclassified conflict",
      new ApiError(409, { reason: "destination_exists" }),
      { key: "files.error.conflict" },
    ],
    [
      "maps an oversized file by status",
      new ApiError(413, { reason: "file_too_large" }),
      { key: "files.error.fileTooLarge" },
    ],
    [
      "maps an invalid path",
      new ApiError(422, { reason: "invalid_path" }),
      { key: "files.error.invalidPath" },
    ],
    [
      "maps a directory targeted as a file",
      new ApiError(422, { reason: "is_a_directory" }),
      { key: "files.error.isDirectory" },
    ],
    [
      "maps a file targeted as a directory",
      new ApiError(422, { reason: "not_a_directory" }),
      { key: "files.error.notDirectory" },
    ],
    [
      "maps a refused symlink",
      new ApiError(422, { reason: "symlink_refused" }),
      { key: "files.error.symlinkRefused" },
    ],
    [
      "maps an overlong name",
      new ApiError(422, { reason: "name_too_long" }),
      { key: "files.error.nameTooLong" },
    ],
    [
      "maps a platform-managed key with its targeted key",
      new ApiError(422, {
        reason: "platform_managed_key",
        key: "rcon.password",
      }),
      {
        key: "files.error.platformManagedKey",
        params: { key: "rcon.password" },
      },
    ],
    [
      "falls back when a platform-managed key has no target",
      new ApiError(422, { reason: "platform_managed_key" }),
      { key: "files.error.invalidInput" },
    ],
    [
      "maps a platform-managed path",
      new ApiError(422, { reason: "platform_managed_path" }),
      { key: "files.error.platformManagedPath" },
    ],
    [
      "maps an otherwise invalid input",
      new ApiError(422, { reason: "unknown_validation_reason" }),
      { key: "files.error.invalidInput" },
    ],
    [
      "maps an unavailable worker by status",
      new ApiError(503, { reason: "worker_unavailable" }),
      { key: "files.error.workerUnavailable" },
    ],
    [
      "falls back for an unknown API status",
      new ApiError(500, { reason: "internal_error" }),
      { key: "files.error.generic" },
    ],
    [
      "falls back for a non-API error",
      new Error("boom"),
      { key: "files.error.generic" },
    ],
  ])("%s", (_name, error, expected) => {
    expect(fileOperationErrorPresentation(error)).toEqual(expected);
  });
});
