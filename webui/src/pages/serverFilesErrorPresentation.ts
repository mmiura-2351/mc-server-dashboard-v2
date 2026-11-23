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

import { ApiError, CREDENTIAL_REQUIRED_FOR_BODY } from "../api/client.ts";
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
        // A write the stop's Worker still holds the server for (#3998):
        // waiting for the release is the remedy, not stopping it (#4003).
        case "server_assigned":
          return { key: "common.serverAssigned" };
        default:
          return { key: "files.error.conflict" };
      }
    case 413:
      // A body refused for want of a credential is a lapsed session that the
      // client's one refresh could not mend, not a file over the cap.
      return error.reason === CREDENTIAL_REQUIRED_FOR_BODY
        ? { key: "login.sessionExpired" }
        : { key: "files.error.fileTooLarge" };
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
        // The member-name rule's other refusals (issue #3973): the remedy is a
        // shallower, shorter or other name, which the generic message would not
        // say.
        case "path_too_deep":
          return { key: "files.error.pathTooDeep" };
        case "path_too_long":
          return { key: "files.error.pathTooLong" };
        case "invalid_name_character":
          return { key: "files.error.invalidNameCharacter" };
        // An upload's file name no file can carry (issue #3922): renaming the
        // file is the remedy, which the generic message would not say.
        case "invalid_filename":
          return { key: "files.error.invalidFilename" };
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
        case "invalid_archive":
          // An extract upload whose archive does not read back (issue #3769):
          // the generic message would not say the file itself is the problem.
          return { key: "files.error.invalidArchive" };
        default:
          return { key: "files.error.invalidInput" };
      }
    case 503:
      switch (error.reason) {
        // The store holding the files, not the server's Worker, did not
        // answer. Without this case the message would blame an agent that is
        // fine.
        case "storage_unavailable":
          return { key: "files.error.storageUnavailable" };
        // Too many archive uploads at once (issue #3843): nothing was written,
        // and sending it again shortly is the remedy.
        case "upload_busy":
          return { key: "files.error.uploadBusy" };
        // The API's database is down (issue #4010), not the server's Worker.
        case "database_unavailable":
          return { key: "common.databaseUnavailable" };
        default:
          return { key: "files.error.workerUnavailable" };
      }
    default:
      return { key: "files.error.generic" };
  }
}

/**
 * Select the toast a failed save of an open file shows in place of the
 * operation's, or `undefined` when it shows the operation's (issue #3973). The
 * file exists, so a name the member-name rule refuses was created before the
 * rule or by a running server: renaming the file is the remedy, not choosing
 * another name.
 */
export function fileSaveErrorPresentation(
  error: unknown,
): FileErrorPresentation | undefined {
  if (!(error instanceof ApiError) || error.status !== 422) {
    return undefined;
  }
  switch (error.reason) {
    case "path_too_deep":
    case "name_too_long":
    case "path_too_long":
    case "invalid_name_character":
      return { key: "files.error.nameNoLongerAllowed" };
    default:
      return undefined;
  }
}

/**
 * Select the toast a failed rollback shows in place of the operation's, or
 * `undefined` when it shows the operation's (issue #4147). A rollback refused
 * because the server is not stopped names the rollback, not the upload and
 * folder writes the operation's wording is about.
 */
export function fileRollbackErrorPresentation(
  error: unknown,
): FileErrorPresentation | undefined {
  return error instanceof ApiError &&
    error.status === 409 &&
    error.reason === "server_not_stopped"
    ? { key: "files.error.rollbackServerMustBeStopped" }
    : undefined;
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
