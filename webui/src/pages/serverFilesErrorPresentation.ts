/**
 * Files-tab error presentation contract.
 *
 * The Files API has file-system-specific reasons and one reason with a
 * targeted `key` extension. It deliberately stays separate from the Plugins
 * contract: their fallback status handling and presentation semantics differ.
 *
 * 403 is intentionally absent. It refreshes permissions through
 * `useOnForbidden`, and `content_dir_protected` changes the visible notice;
 * the component handles both control-flow cases before it asks this pure mapper
 * for a toast presentation.
 */

import { ApiError } from "../api/client.ts";
import type { TranslationKey } from "../i18n/index.ts";
import { FileTooLargeToEditError, UnencodableTextError } from "./fileText.ts";

export interface FileErrorPresentation {
  key: TranslationKey;
  params?: Record<string, string | number>;
}

/** Select the Files-tab toast presentation for a failed operation. */
export function fileOperationErrorPresentation(
  error: unknown,
): FileErrorPresentation {
  if (error instanceof UnencodableTextError) {
    return { key: "files.error.unencodableText" };
  }
  if (error instanceof FileTooLargeToEditError) {
    return { key: "files.error.editTooLarge" };
  }
  if (!(error instanceof ApiError)) {
    return { key: "files.error.generic" };
  }

  switch (error.status) {
    case 404:
      return { key: "files.error.notFound" };
    case 409:
      switch (error.reason) {
        case "server_unsettled":
        case "server_not_stopped":
          return { key: "files.error.serverMustBeStopped" };
        case "server_busy":
          return { key: "files.error.serverBusy" };
        default:
          return { key: "files.error.conflict" };
      }
    case 413:
      return { key: "files.error.fileTooLarge" };
    case 422:
      switch (error.reason) {
        case "invalid_path":
          return { key: "files.error.invalidPath" };
        case "is_a_directory":
          return { key: "files.error.isDirectory" };
        case "not_a_directory":
          return { key: "files.error.notDirectory" };
        case "symlink_refused":
          return { key: "files.error.symlinkRefused" };
        case "name_too_long":
          return { key: "files.error.nameTooLong" };
        case "platform_managed_key":
          return error.key === undefined
            ? { key: "files.error.invalidInput" }
            : {
                key: "files.error.platformManagedKey",
                params: { key: error.key },
              };
        case "platform_managed_path":
          return { key: "files.error.platformManagedPath" };
        case "reserved_name":
          return { key: "files.error.reservedName" };
        default:
          return { key: "files.error.invalidInput" };
      }
    case 503:
      // The store holding the files, not the server's Worker, did not answer.
      // Without this case the message would blame an agent that is fine.
      return error.reason === "storage_unavailable"
        ? { key: "files.error.storageUnavailable" }
        : { key: "files.error.workerUnavailable" };
    default:
      return { key: "files.error.generic" };
  }
}

/**
 * Select the viewer's inline message for a file that could not be read
 * (#3361). The read shares the operations' reasons except two whose operation
 * wording is about changes: an unsettled server (the operations say "stop the
 * server") and the size cap (the operations quote the upload and edit caps).
 * The fallback names the failed open instead of "something went wrong".
 */
export function fileReadErrorPresentation(
  error: unknown,
): FileErrorPresentation {
  if (error instanceof ApiError) {
    if (error.status === 409 && error.reason === "server_unsettled") {
      return { key: "files.error.readUnsettled" };
    }
    if (error.status === 413) {
      return { key: "files.error.readTooLarge" };
    }
  }
  const presentation = fileOperationErrorPresentation(error);
  return presentation.key === "files.error.generic"
    ? { key: "files.openError" }
    : presentation;
}

/**
 * Select the listing's inline message for a directory that could not be
 * listed (#3323). A storage outage gets the operations' storage message, so
 * the tab names the store the same way its operations do. Anything else keeps
 * the generic list message.
 */
export function fileListErrorPresentation(
  error: unknown,
): FileErrorPresentation {
  return error instanceof ApiError &&
    error.status === 503 &&
    error.reason === "storage_unavailable"
    ? { key: "files.error.storageUnavailable" }
    : { key: "files.listError" };
}
