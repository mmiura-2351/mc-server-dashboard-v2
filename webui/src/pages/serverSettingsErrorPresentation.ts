/** Slug errors land inline; other settings, delete, and export failures use toasts.
 * Callers handle 403 capability refresh before this mapper. */

import type { TranslationKey } from "../i18n/index.ts";

export interface SettingsErrorPresentation {
  target: "slug" | "toast";
  key: TranslationKey;
}

const SETTINGS_ERROR_KEY: Record<string, TranslationKey> = {
  server_not_stopped: "serverDetail.error.notStopped",
  server_unsettled: "serverDetail.error.unsettled",
  port_taken: "serverDetail.error.portTaken",
  port_out_of_range: "serverDetail.error.portOutOfRange",
  invalid_snapshot_interval: "serverDetail.error.invalidSnapshotInterval",
  retired_config_key: "serverDetail.error.retiredConfigKey",
  invalid_memory_limit: "serverDetail.error.invalidMemoryLimit",
  invalid_cpu_allocation: "serverDetail.error.invalidCpuAllocation",
  // JSON-valued overrides can trigger null, depth, size, and lone-surrogate config guards.
  config_too_large: "serverDetail.error.configTooLarge",
  config_null_value: "serverDetail.error.configNullValue",
  config_invalid_shape: "serverDetail.error.configInvalidShape",
  config_lone_surrogate: "serverDetail.error.configLoneSurrogate",
};

export function serverSettingsErrorPresentation(
  reason: string | undefined,
): SettingsErrorPresentation {
  switch (reason) {
    case "invalid_slug":
      return { target: "slug", key: "serverDetail.settings.slugInvalid" };
    case "slug_taken":
      return { target: "slug", key: "serverDetail.settings.slugTaken" };
  }
  const key = reason === undefined ? undefined : SETTINGS_ERROR_KEY[reason];
  return { target: "toast", key: key ?? "serverDetail.error.generic" };
}
