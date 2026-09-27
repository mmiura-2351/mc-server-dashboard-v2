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
import { UnencodableTextError } from "./fileText.ts";

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
        default:
          return { key: "files.error.invalidInput" };
      }
    case 503:
      return { key: "files.error.workerUnavailable" };
    default:
      return { key: "files.error.generic" };
  }
}
