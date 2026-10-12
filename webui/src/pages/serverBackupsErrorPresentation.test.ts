// @vitest-environment node
import { describe, expect, it } from "vitest";
import {
  backupErrorPresentation,
  backupRestoreErrorPresentation,
} from "./serverBackupsErrorPresentation.ts";

describe("backupErrorPresentation", () => {
  it.each([
    [409, "server_unsettled", "backups.error.unsettled"],
    [409, "server_not_stopped", "backups.error.serverMustBeStopped"],
    [409, "server_busy", "backups.error.serverBusy"],
    [409, "worker_busy", "backups.error.serverBusy"],
    [422, "invalid_archive", "backups.error.invalidArchive"],
    [422, "platform_managed_path", "backups.error.platformManagedPath"],
    [503, "worker_unavailable", "backups.error.workerUnavailable"],
  ] as const)("maps a %i %s to its specific message", (status, reason, key) => {
    expect(backupErrorPresentation(status, reason)).toBe(key);
  });

  it("names the object store for a 503 storage_unavailable", () => {
    expect(backupErrorPresentation(503, "storage_unavailable")).toBe(
      "backups.error.storageUnavailable",
    );
  });

  it.each([
    [413, "backups.error.tooLarge"],
    [503, "backups.error.workerUnavailable"],
  ] as const)("maps a reasonless %i by status", (status, key) => {
    expect(backupErrorPresentation(status, undefined)).toBe(key);
  });

  it.each([
    ["a mutation", undefined, "backups.error.generic"],
    ["the listing", "backups.loadError", "backups.loadError"],
    ["the statistics", "backups.stats.loadError", "backups.stats.loadError"],
  ] as const)(
    "falls back to the context message for %s",
    (_label, fallback, key) => {
      expect(backupErrorPresentation(404, "not_found", fallback)).toBe(key);
      expect(backupErrorPresentation(undefined, undefined, fallback)).toBe(key);
    },
  );
});

describe("backupRestoreErrorPresentation", () => {
  it("names the restore precondition for server_not_stopped", () => {
    expect(backupRestoreErrorPresentation(409, "server_not_stopped")).toBe(
      "backups.error.notStopped",
    );
  });

  it("names the unreadable archive for backup_unreadable", () => {
    expect(backupRestoreErrorPresentation(409, "backup_unreadable")).toBe(
      "backups.error.unreadable",
    );
  });

  it.each([
    [503, "storage_unavailable", "backups.error.storageUnavailable"],
    [404, "not_found", "backups.error.generic"],
  ] as const)(
    "otherwise uses the shared mapping for a %i %s",
    (status, reason, key) => {
      expect(backupRestoreErrorPresentation(status, reason)).toBe(key);
    },
  );
});
