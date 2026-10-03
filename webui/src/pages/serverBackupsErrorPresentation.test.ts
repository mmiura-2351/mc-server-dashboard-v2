// @vitest-environment node
// DOM-free contract test: the Backups tab renders the selected key.
import { describe, expect, it } from "vitest";
import {
  backupErrorPresentation,
  backupRestoreErrorPresentation,
} from "./serverBackupsErrorPresentation.ts";

describe("backupErrorPresentation", () => {
  // `server_busy` (API lock) and `worker_busy` (Worker refusal) are one remedy,
  // so they share one message (issue #2436). A member under the root
  // server.properties path names what to remove (issue #2869).
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

  // The reason wins over the bare-503 status arm, which would blame the server
  // host for an object-store outage (issue #2378).
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

  // Mutations fall back to the generic message; the listing and statistics
  // loads pass their own so an unmapped failure still reads as a load failure
  // (issue #2554).
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
  // The server moved out of stopped mid-flight: the restore message names
  // restoring, not the generic stopped-only operation.
  it("names the restore precondition for server_not_stopped", () => {
    expect(backupRestoreErrorPresentation(409, "server_not_stopped")).toBe(
      "backups.error.notStopped",
    );
  });

  // The backup was recorded unreadable (#2374) — e.g. by a sweep that ran while
  // the dialog was open: the message names why it cannot be restored rather than
  // a generic failure the user would retry.
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
