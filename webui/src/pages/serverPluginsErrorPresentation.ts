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

import { ApiError } from "../api/client.ts";
import type { TranslationKey } from "../i18n/index.ts";

// `file_too_large` (413-only) and `worker_unavailable` (503-only) deliberately
// use the status fallbacks below: their reason-specific messages would be
// identical, while each remaining reason changes the presentation.
const PLUGIN_REASON_PRESENTATION: Record<string, TranslationKey> = {
  plugin_already_exists: "plugins.error.alreadyExists",
  server_not_stopped: "plugins.error.notStopped",
  server_unsettled: "plugins.error.unsettled",
  server_busy: "plugins.error.busy",
  invalid_path: "plugins.error.invalidPath",
  catalog_upstream_failed: "plugins.error.catalogUpstreamFailed",
  catalog_project_not_found: "plugins.error.catalogNotFound",
  checksum_mismatch: "plugins.error.checksumMismatch",
  unsupported_server_type: "plugins.error.unsupportedServerType",
  invalid_side: "plugins.error.invalidSide",
  invalid_display_name: "plugins.error.invalidDisplayName",
  bedrock_port_range_exhausted: "plugins.error.bedrockPortRangeExhausted",
  bedrock_port_taken: "plugins.error.bedrockPortTaken",
  not_found: "plugins.error.notFound",
};

/** Select the Plugins-tab toast key for a failed operation. */
export function pluginErrorPresentation(error: unknown): TranslationKey {
  if (!(error instanceof ApiError)) {
    return "plugins.error.generic";
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
