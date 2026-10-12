import type { TranslationKey } from "../i18n/index.ts";

export function backupErrorPresentation(
  status: number | undefined,
  reason: string | undefined,
  fallback: TranslationKey = "backups.error.generic",
): TranslationKey {
  switch (reason) {
    case "server_unsettled":
      return "backups.error.unsettled";
    case "server_not_stopped":
      return "backups.error.serverMustBeStopped";
    case "server_busy":
    case "worker_busy":
      // Both lock contention and Worker busy mean backup creation was refused; wait for the operation to clear.
      return "backups.error.serverBusy";
    case "invalid_archive":
      return "backups.error.invalidArchive";
    case "platform_managed_path":
      return "backups.error.platformManagedPath";
    case "worker_unavailable":
      return "backups.error.workerUnavailable";
    case "storage_unavailable":
      // Prefer the storage reason over the 503 fallback so the message does not blame the server host.
      return "backups.error.storageUnavailable";
  }

  switch (status) {
    case 413:
      return "backups.error.tooLarge";
    case 503:
      return "backups.error.workerUnavailable";
  }

  return fallback;
}

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
