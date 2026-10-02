/**
 * Console-tab command error presentation (issue #2450).
 *
 * A failed console command appends one transcript line under the echoed
 * command. The causes the command route can report call for different next
 * actions, so each gets its own line instead of a single "Command failed.".
 *
 * This is deliberately its own map rather than `lifecycleErrors.ts`: those
 * strings are toasts beside a state badge, and several are wrong for a console
 * line — `worker_unavailable` there says "try again", but a console command
 * whose dispatch timed out may already have run, and `server_not_running` there
 * is the "state changed — refreshed" toast, which names nothing in a transcript.
 *
 * 403 is intentionally absent because `useOnForbidden` owns its session and
 * permission-refresh side effect before a caller reaches this mapper.
 */

import { ApiError } from "../api/client.ts";
import type { TranslationKey } from "../i18n/index.ts";

// Every reason POST .../command can render (servers/api/servers.py
// send_server_command, servers/application/lifecycle.py SendServerCommand):
//
// - `worker_unavailable` (503) — the dispatch timed out or lost the Worker
//   session, so whether the line ran is unknown and a blind resend may run it
//   twice.
// - `server_not_running` (409) — the API's observed state is not running, or
//   the Worker holds no instance for the id. Nothing was sent.
// - `failed_stop_orphan` (409) — the Worker holds an instance whose stop it
//   could not confirm (issue #2466); the process may still be alive, so this
//   must not read as "not running". Nothing was sent.
// - `command_failed` (409) — the Worker reached a running server but its RCON
//   connection failed (open, auth, or the round trip itself), which is the only
//   unclassified failure `handleServerCommand` produces. A command the server
//   understood and refused (an unknown command, bad syntax) is NOT this: the
//   server answers it over RCON as ordinary output, which arrives as a 200 and
//   is already shown verbatim.
//
// Anything else (a 404 for a deleted server, a network error) keeps the generic
// line.
const CONSOLE_REASON_PRESENTATION: Record<string, TranslationKey> = {
  worker_unavailable: "serverDetail.console.error.workerUnavailable",
  server_not_running: "serverDetail.console.error.notRunning",
  failed_stop_orphan: "serverDetail.console.error.failedStopOrphan",
  command_failed: "serverDetail.console.error.rconFailed",
};

/** Select the Console-tab transcript line for a failed command. */
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
