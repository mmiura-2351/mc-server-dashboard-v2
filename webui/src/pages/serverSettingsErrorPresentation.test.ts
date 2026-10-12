// @vitest-environment node
import { describe, expect, it } from "vitest";
import { serverSettingsErrorPresentation } from "./serverSettingsErrorPresentation.ts";

describe("serverSettingsErrorPresentation", () => {
  it.each([
    ["server_not_stopped", "serverDetail.error.notStopped"],
    ["server_unsettled", "serverDetail.error.unsettled"],
    ["port_taken", "serverDetail.error.portTaken"],
    ["port_out_of_range", "serverDetail.error.portOutOfRange"],
    ["invalid_snapshot_interval", "serverDetail.error.invalidSnapshotInterval"],
    ["retired_config_key", "serverDetail.error.retiredConfigKey"],
    ["invalid_memory_limit", "serverDetail.error.invalidMemoryLimit"],
    ["invalid_cpu_allocation", "serverDetail.error.invalidCpuAllocation"],
    ["config_too_large", "serverDetail.error.configTooLarge"],
    ["config_null_value", "serverDetail.error.configNullValue"],
    ["config_invalid_shape", "serverDetail.error.configInvalidShape"],
    ["config_lone_surrogate", "serverDetail.error.configLoneSurrogate"],
  ] as const)("toasts %s with its specific message", (reason, key) => {
    expect(serverSettingsErrorPresentation(reason)).toEqual({
      target: "toast",
      key,
    });
  });

  it.each([
    ["invalid_slug", "serverDetail.settings.slugInvalid"],
    ["slug_taken", "serverDetail.settings.slugTaken"],
  ] as const)("puts %s inline on the slug field", (reason, key) => {
    expect(serverSettingsErrorPresentation(reason)).toEqual({
      target: "slug",
      key,
    });
  });

  it.each([
    ["an unmapped reason", "not_found"],
    ["a failure without a reason", undefined],
  ])("falls back to the generic toast for %s", (_label, reason) => {
    expect(serverSettingsErrorPresentation(reason)).toEqual({
      target: "toast",
      key: "serverDetail.error.generic",
    });
  });
});
