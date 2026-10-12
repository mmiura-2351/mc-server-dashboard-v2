/** Select loader-specific plugin messages here; the caller supplies the content noun.
 * Callers handle 403 capability refresh before this mapper. */

import { ApiError } from "../api/client.ts";
import type { TranslationKey } from "../i18n/index.ts";

// file_too_large and worker_unavailable use status fallbacks because their messages would be identical.
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
  // A JAR-store outage needs a storage message rather than the Worker-unavailable fallback.
  storage_unavailable: "plugins.error.storageUnavailable",
  not_found: "plugins.error.notFound",
};

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
