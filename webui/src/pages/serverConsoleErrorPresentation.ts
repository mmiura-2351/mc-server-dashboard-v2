/** Console failures need transcript messages, not lifecycle toasts; a timed-out command may already have run.
 * Callers handle 403 capability refresh before invoking this mapper. */

import { ApiError } from "../api/client.ts";
import type { TranslationKey } from "../i18n/index.ts";

// worker_unavailable leaves execution unknown; server_not_running and failed_stop_orphan send no command.
// command_failed covers RCON failure; Minecraft command refusals return normal output instead.
const CONSOLE_REASON_PRESENTATION: Record<string, TranslationKey> = {
  worker_unavailable: "serverDetail.console.error.workerUnavailable",
  server_not_running: "serverDetail.console.error.notRunning",
  failed_stop_orphan: "serverDetail.console.error.failedStopOrphan",
  command_failed: "serverDetail.console.error.rconFailed",
};

export function consoleCommandErrorPresentation(
  error: unknown,
): TranslationKey {
  if (error instanceof ApiError && error.reason !== undefined) {
    const presentation = CONSOLE_REASON_PRESENTATION[error.reason];
    if (presentation !== undefined) {
      return presentation;
    }
  }
  return "serverDetail.commandFailed";
}
