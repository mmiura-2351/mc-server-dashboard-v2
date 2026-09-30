/**
 * Server-detail Settings error presentation contract (WEBUI_SPEC.md 6.9).
 *
 * The settings save, the danger-zone delete and the danger-zone export share
 * one handler. A join-address refusal lands inline on the slug field; every
 * other failure is a toast.
 *
 * 403 is intentionally absent: `useOnForbidden` owns its capability refresh
 * before the handler reaches this mapper.
 */

import type { TranslationKey } from "../i18n/index.ts";

export interface SettingsErrorPresentation {
  target: "slug" | "toast";
  key: TranslationKey;
}

// 422 carries a port reason (port_out_of_range) or the snapshot cadence reason
// (invalid_snapshot_interval), 409 the at-rest gate (server_not_stopped) and
// export the unsettled gate.
const SETTINGS_ERROR_KEY: Record<string, TranslationKey> = {
  server_not_stopped: "serverDetail.error.notStopped",
  server_unsettled: "serverDetail.error.unsettled",
  port_taken: "serverDetail.error.portTaken",
  port_out_of_range: "serverDetail.error.portOutOfRange",
  invalid_snapshot_interval: "serverDetail.error.invalidSnapshotInterval",
  retired_config_key: "serverDetail.error.retiredConfigKey",
  invalid_memory_limit: "serverDetail.error.invalidMemoryLimit",
  invalid_cpu_allocation: "serverDetail.error.invalidCpuAllocation",
  // The config-blob guard (issue #94). This editor reads every value as
  // JSON, so all four of its rules describe something a row can carry: a
  // typed `null`, a literal nested past the depth cap, an oversized paste,
  // and an unpaired surrogate escape.
  config_too_large: "serverDetail.error.configTooLarge",
  config_null_value: "serverDetail.error.configNullValue",
  config_invalid_shape: "serverDetail.error.configInvalidShape",
  config_lone_surrogate: "serverDetail.error.configLoneSurrogate",
};

/** Select where and how the Settings tab shows a failed save/delete/export. */
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
