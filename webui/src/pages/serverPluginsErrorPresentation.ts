/**
 * Plugins-tab error presentation contract.
 *
 * Plugin and catalog operations share one toast handler, but their reason set
 * and loader-aware nouns are specific to this feature. The caller applies the
 * noun after this pure mapper selects the translation key.
 *
 * 403 is intentionally absent because `useOnForbidden` owns its session and
 * permission-refresh side effect before a caller reaches this mapper.
 */

import { ApiError, CREDENTIAL_REQUIRED_FOR_BODY } from "../api/client.ts";
import type { TranslationKey } from "../i18n/index.ts";

// `file_too_large` (413-only) and `worker_unavailable` (503-only) deliberately
// use the status fallbacks below: their reason-specific messages would be
// identical, while each remaining reason changes the presentation.
const PLUGIN_REASON_PRESENTATION: Record<string, TranslationKey> = {
  plugin_already_exists: "plugins.error.alreadyExists",
  server_not_stopped: "plugins.error.notStopped",
  server_unsettled: "plugins.error.unsettled",
  server_busy: "plugins.error.busy",
  // A change the stop's Worker still holds the server for (#3998, #4003).
  server_assigned: "common.serverAssigned",
  invalid_path: "plugins.error.invalidPath",
  catalog_upstream_failed: "plugins.error.catalogUpstreamFailed",
  catalog_project_not_found: "plugins.error.catalogNotFound",
  checksum_mismatch: "plugins.error.checksumMismatch",
  unsupported_server_type: "plugins.error.unsupportedServerType",
  invalid_side: "plugins.error.invalidSide",
  invalid_display_name: "plugins.error.invalidDisplayName",
  bedrock_port_range_exhausted: "plugins.error.bedrockPortRangeExhausted",
  bedrock_port_taken: "plugins.error.bedrockPortTaken",
  // Also a 503, but the jar store rather than the server's Worker: the status
  // fallback below would blame an agent that is fine.
  storage_unavailable: "plugins.error.storageUnavailable",
  // The API's database is down (issue #4010), for the same reason.
  database_unavailable: "common.databaseUnavailable",
  not_found: "plugins.error.notFound",
  plugin_jar_missing: "plugins.error.jarMissing",
  // A 413 for a lapsed session the client's one refresh could not mend, not a
  // jar over the cap.
  [CREDENTIAL_REQUIRED_FOR_BODY]: "login.sessionExpired",
};

/**
 * Where the jar an operation installs comes from: the user's own file, or a
 * catalog. An operation on an installed jar installs none.
 */
export type PluginJarSource = "upload" | "catalog";

/**
 * Select the Plugins-tab toast key for a failed operation.
 *
 * `invalid_filename` names the jar's file name (#3988): the user can rename an
 * upload, but not a catalog's file.
 */
export function pluginErrorPresentation(
  error: unknown,
  jarSource: PluginJarSource,
): TranslationKey {
  if (!(error instanceof ApiError)) {
    return "plugins.error.generic";
  }

  if (error.reason === "invalid_filename") {
    return jarSource === "upload"
      ? "plugins.error.invalidFilename"
      : "plugins.error.catalogInvalidFilename";
  }
  if (error.reason !== undefined) {
    const presentation = PLUGIN_REASON_PRESENTATION[error.reason];
    if (presentation !== undefined) {
      return presentation;
    }
  }

  switch (error.status) {
    case 413:
      return "plugins.error.tooLarge";
    case 503:
      return "plugins.error.workerUnavailable";
    default:
      return "plugins.error.generic";
  }
}
