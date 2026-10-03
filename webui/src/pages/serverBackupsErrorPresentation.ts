/**
 * Backups-tab error presentation contract (WEBUI_SPEC.md 6.7).
 *
 * Create, upload, download, delete, the restore dialog's stop and the two load
 * queries share one reason/status mapping; the caller supplies the fallback
 * for its context. Restore names its own stopped-only precondition.
 *
 * 403 is intentionally absent: `useOnForbidden` owns its capability refresh,
 * and the restore dialog also closes on it, before a caller reaches here.
 */

import type { TranslationKey } from "../i18n/index.ts";

/**
 * Select the Backups-tab message for a failed operation or load. `fallback` is
 * the generic message for mutations; the load guards pass a load-context one so
 * an unmapped failure still reads as a load failure (#2554).
 */
export function backupErrorPresentation(
  status: number | undefined,
  reason: string | undefined,
  fallback: TranslationKey = "backups.error.generic",
): TranslationKey {
  // Check reason first (most specific).
  switch (reason) {
    case "server_unsettled":
      return "backups.error.unsettled";
    case "server_not_stopped":
      return "backups.error.serverMustBeStopped";
    case "server_busy":
    case "worker_busy":
      // Two layers, one remedy (issue #2436). `server_busy` is API-side
      // lifecycle-lock contention; `worker_busy` is the Worker refusing a
      // SnapshotTrigger because another mutating command for this server is
      // already in flight — the create was refused without being applied and
      // clears on its own, so the operator's only move for either is to wait.
      // Naming which layer was busy would be internals they cannot act on, the
      // same call the lifecycle surfaces made (lifecycleErrors.ts).
      return "backups.error.serverBusy";
    case "invalid_archive":
      return "backups.error.invalidArchive";
    case "platform_managed_path":
      // An uploaded archive whose member lands under the root server.properties
      // path (issue #2869): a verdict about the archive's contents, so the
      // generic toast would hide the one member the user has to remove.
      return "backups.error.platformManagedPath";
    case "worker_unavailable":
      return "backups.error.workerUnavailable";
    case "storage_unavailable":
      // The object store, not the server host, is down (issue #2378). Without
      // this case the 503 below would blame the host for a storage outage.
      return "backups.error.storageUnavailable";
  }

  // Check status (less specific).
  switch (status) {
    case 413:
      return "backups.error.tooLarge";
    case 503:
      return "backups.error.workerUnavailable";
  }

  return fallback;
}

/**
 * Select the restore dialog's message. A 409 `server_not_stopped` means the
 * state changed mid-flight, so it names restoring rather than the generic
 * stopped-only operation. A 409 `backup_unreadable` (#2374) means the backup's
 * archive cannot be read back, so no retry or override will restore it.
 */
export function backupRestoreErrorPresentation(
  status: number | undefined,
  reason: string | undefined,
): TranslationKey {
  if (reason === "server_not_stopped") {
    return "backups.error.notStopped";
  }
  if (reason === "backup_unreadable") {
    return "backups.error.unreadable";
  }
  return backupErrorPresentation(status, reason);
}
