// @vitest-environment node
import { describe, expect, it } from "vitest";
import {
  serverCreateErrorPresentation,
  serverImportErrorPresentation,
} from "./serverCreateErrorPresentation.ts";

describe("serverCreateErrorPresentation", () => {
  it.each([
    ["port_taken", "serverCreate.error.port_taken"],
    ["port_out_of_range", "serverCreate.error.port_out_of_range"],
    ["server_name_exists", "serverCreate.error.server_name_exists"],
    ["unknown_version", "serverCreate.error.unknown_version"],
    ["invalid_memory_limit", "serverCreate.error.invalid_memory_limit"],
    ["invalid_cpu_allocation", "serverCreate.error.invalid_cpu_allocation"],
    ["config_too_large", "serverCreate.error.config_too_large"],
    ["config_lone_surrogate", "serverCreate.error.config_lone_surrogate"],
  ] as const)("toasts %s with its specific message", (reason, key) => {
    expect(serverCreateErrorPresentation(reason)).toEqual({
      target: "toast",
      key,
    });
  });

  it.each([
    ["invalid_server_name", "name", "serverCreate.error.invalid_server_name"],
    ["invalid_slug", "slug", "serverCreate.error.invalid_slug"],
    ["slug_taken", "slug", "serverCreate.error.slug_taken"],
  ] as const)("puts %s inline on the %s field", (reason, target, key) => {
    expect(serverCreateErrorPresentation(reason)).toEqual({ target, key });
  });

  it.each([
    ["an unmapped reason", "validation_error"],
    ["a failure without a reason", undefined],
  ])("falls back to the generic toast for %s", (_label, reason) => {
    expect(serverCreateErrorPresentation(reason)).toEqual({
      target: "toast",
      key: "serverCreate.genericError",
    });
  });
});

describe("serverImportErrorPresentation", () => {
  it("names the archive size limit for a 413", () => {
    expect(serverImportErrorPresentation(413, undefined)).toEqual({
      target: "toast",
      key: "serverCreate.import.tooLarge",
    });
  });

  // ZIP import skips directory entries and allocates its own slug; collision feedback must ask for retry.
  it.each([
    [
      422,
      "invalid_export_metadata",
      "serverCreate.import.error.invalid_export_metadata",
    ],
    [
      422,
      "platform_managed_path",
      "serverCreate.import.error.platform_managed_path",
    ],
    [409, "slug_taken", "serverCreate.import.error.slug_taken"],
  ] as const)(
    "toasts a %i %s with the import message",
    (status, reason, key) => {
      expect(serverImportErrorPresentation(status, reason)).toEqual({
        target: "toast",
        key,
      });
    },
  );

  it("shares the create toast for a create reason (#3022)", () => {
    expect(serverImportErrorPresentation(409, "port_taken")).toEqual({
      target: "toast",
      key: "serverCreate.error.port_taken",
    });
  });

  it("keeps a name reason on the import name field", () => {
    expect(serverImportErrorPresentation(422, "invalid_server_name")).toEqual({
      target: "name",
      key: "serverCreate.error.invalid_server_name",
    });
  });

  it("toasts a create slug-field reason", () => {
    expect(serverImportErrorPresentation(422, "invalid_slug")).toEqual({
      target: "toast",
      key: "serverCreate.error.invalid_slug",
    });
  });

  it.each([
    ["an unmapped reason", 503, "slug_exhausted"],
    ["a failure that is not an API problem", undefined, undefined],
  ])("falls back to the generic toast for %s", (_label, status, reason) => {
    expect(serverImportErrorPresentation(status, reason)).toEqual({
      target: "toast",
      key: "serverCreate.genericError",
    });
  });
});
