// @vitest-environment node
// DOM-free contract test: the Backups tab renders the selected key.
import { describe, expect, it } from "vitest";
import { en } from "../i18n/en.ts";
import { ja } from "../i18n/ja.ts";
import {
  backupErrorDetail,
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
    // A create the stop's Worker still holds the server for (#3998, #4003).
    [409, "server_assigned", "common.serverAssigned"],
    // A running server's backup cannot quiesce the world when its
    // server.properties is refused (issue #3347).
    [
      409,
      "server_properties_unreadable",
      "backups.error.serverPropertiesUnreadable",
    ],
    [422, "invalid_archive", "backups.error.invalidArchive"],
    [422, "platform_managed_path", "backups.error.platformManagedPath"],
    // A member at the Worker's generation-marker name (issue #3433).
    [422, "reserved_name", "backups.error.reservedName"],
    [503, "worker_unavailable", "backups.error.workerUnavailable"],
  ] as const)("maps a %i %s to its specific message", (status, reason, key) => {
    expect(backupErrorPresentation(status, reason)).toBe(key);
  });

  // A create of a server held by a refused final snapshot names the refusal,
  // since that hold has no bound (#4057, #4146).
  it.each(["busy", "unavailable"] as const)(
    "names a %s refusal for a 409 server_assigned",
    (refusal) => {
      expect(
        backupErrorPresentation(409, "server_assigned", undefined, refusal),
      ).toBe(`common.serverHeld.${refusal}`);
    },
  );

  // The reason wins over the bare-503 status arm, which would blame the server
  // host for an object-store outage (issue #2378).
  it("names the object store for a 503 storage_unavailable", () => {
    expect(backupErrorPresentation(503, "storage_unavailable")).toBe(
      "backups.error.storageUnavailable",
    );
  });

  // The API's database, not the server host, is down (issue #4010).
  it("names the database for a 503 database_unavailable", () => {
    const key = backupErrorPresentation(503, "database_unavailable");
    expect(key).toBe("common.databaseUnavailable");
    expect(en[key]).toMatch(/database.*try again/i);
    expect(ja[key]).toMatch(/データベース.*お試しください/);
  });

  // A working set whose archive would pass the 8 GiB backup cap (issue #3859):
  // nothing was written, and trying again cannot help until the server's files
  // are smaller, so the message names the limit instead of asking for a retry.
  it("names the 8 GiB limit for a 409 working_set_too_large", () => {
    const key = backupErrorPresentation(409, "working_set_too_large");
    expect(key).toBe("backups.error.workingSetTooLarge");
    expect(en[key]).toMatch(/8 GiB/);
    expect(en[key]).not.toMatch(/try again/i);
    expect(ja[key]).toMatch(/8 GiB/);
  });

  // Too many archive uploads at once (issue #3843): nothing was stored, and
  // the remedy is to send it again shortly, not to look at a server host.
  it("names the busy uploads for a 503 upload_busy", () => {
    expect(backupErrorPresentation(503, "upload_busy")).toBe(
      "backups.error.uploadBusy",
    );
  });

  // An upload refused for want of a credential is a lapsed session, not an
  // archive past the cap.
  it("names the lapsed session for a 413 credential_required_for_body", () => {
    expect(backupErrorPresentation(413, "credential_required_for_body")).toBe(
      "login.sessionExpired",
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

describe("backupErrorDetail", () => {
  // Without a detail the invalid-archive message shows alone (issue #3892).
  it.each([
    ["no", undefined],
    ["an empty", ""],
  ])("shows none for an invalid_archive with %s detail", (_label, detail) => {
    expect(backupErrorDetail("invalid_archive", detail)).toBeUndefined();
  });
});

describe("backupRestoreErrorPresentation", () => {
  // The server moved out of stopped mid-flight: the restore message names
  // restoring, not the generic stopped-only operation.
  it("names the restore precondition for server_not_stopped", () => {
    expect(backupRestoreErrorPresentation(409, "server_not_stopped")).toBe(
      "backups.error.notStopped",
    );
  });

  // A restore the stop's Worker still holds the server for (#3998): the
  // server reads stopped, and waiting for the release is the remedy (#4003).
  it("names the release for server_assigned", () => {
    expect(backupRestoreErrorPresentation(409, "server_assigned")).toBe(
      "common.serverAssigned",
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

  // The backup WAS published and only a follow-up step failed (issue #3269):
  // the bare-503 arm would blame the server host, while the remedy is to retry
  // the restore.
  it.each([
    "seed_failed",
    "plugin_reconcile_incomplete",
    "publish_incomplete",
    "restore_incomplete",
  ])("names the incomplete follow-up step for a 503 %s", (reason) => {
    const key = backupRestoreErrorPresentation(503, reason);
    expect(key).toBe("backups.error.restoreIncomplete");
    expect(en[key]).toMatch(/restored.*retry the restore/i);
    expect(ja[key]).toMatch(/復元.*もう一度/);
  });

  // The store could not confirm whether the backup went live (issue #3564):
  // the message must not claim it was restored, only that it may have been.
  it("names the unconfirmed restore for a 503 publish_unknown", () => {
    const key = backupRestoreErrorPresentation(503, "publish_unknown");
    expect(key).toBe("backups.error.restoreOutcomeUnknown");
    expect(en[key]).toMatch(/may have been restored.*retry the restore/i);
    expect(ja[key]).toMatch(/復元された可能性.*もう一度/);
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
