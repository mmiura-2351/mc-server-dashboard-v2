/**
 * Map a lifecycle mutation error to its toast message (WEBUI_SPEC.md 7.4).
 *
 * The API returns 409 for lifecycle races (SPEC 7.4, "state changed —
 * refresh") and for several causes that are not races at all. A non-race whose
 * cause is worth naming gets its own message instead of the misleading generic
 * state-changed toast: the sanitized start/restart-failure categories
 * `port_conflict` / `image_missing` (issue #225), `server_unsettled` — the
 * at-rest precondition on the export mint (issue #2360) — and the two
 * contention reasons `worker_busy` / `server_busy` (issue #2400).
 *
 * Four reasons keep the state-changed treatment. Three are genuine races:
 * `invalid_transition`, `transition_conflict` and `server_not_running` each
 * mean the server really did move, or was never in the state the caller
 * assumed, so refreshing is the right response. `command_failed` is there for a
 * different reason — it is the catch-all for a dispatch failure the Worker did
 * not classify (e.g. an INTERNAL driver error), where nothing necessarily moved
 * but there is also no specific cause to name.
 *
 * 503 responses with a recognized `reason` (`no_eligible_worker`,
 * `worker_unavailable`, `jar_unavailable`) get their own message (issue #1092).
 * All other errors fall back to the generic action-failed toast.
 *
 * 403 is intentionally NOT handled here: it carries a side effect (refetching
 * capabilities) that lives in `useOnForbidden`. Callers run that glue first and
 * only reach this helper for non-403 errors.
 *
 * Returns a `TranslationKey` so both the dashboard quick actions and the
 * server-detail lifecycle controls (#378 Phase 4) share one mapping.
 */

import { ApiError } from "../api/client.ts";
import type { TranslationKey } from "../i18n/index.ts";

// 409 reasons that get a specific message; every other 409 keeps the
// state-changed treatment (see the header for why each one belongs there).
// `port_conflict`, `image_missing` and `worker_busy` are the sanitized dispatch
// categories, mirroring the API's `_SANITIZED_REASONS`
// (servers/application/command_dispatch.py).
// `server_unsettled` is the at-rest precondition on the export mint
// (servers/api/servers.py), which is a standing requirement rather than a race,
// so it names the precondition here too — the same message the danger-zone
// export already shows (issue #2360).
//
// `worker_busy` (the Worker already has a mutating lifecycle command in flight
// for this server; start/stop/restart can all hit it) and `server_busy` (a
// gated op held the API-side lifecycle lock past the acquire budget; start
// only) share one message: both mean the request was refused without being
// applied, both clear on their own once the other operation settles, and the
// operator's only move for either is to retry — naming which layer was busy
// would be Worker/API internals they cannot act on (issue #2400). The files and
// plugins tabs already name `server_busy` this way (`files.error.serverBusy`,
// `plugins.error.busy`); the lifecycle surfaces were the outlier.
const SPECIFIC_409_MESSAGE: Record<string, TranslationKey> = {
  port_conflict: "dashboard.lifecycle.portConflict",
  image_missing: "dashboard.lifecycle.imageMissing",
  server_unsettled: "serverDetail.error.unsettled",
  worker_busy: "dashboard.lifecycle.busy",
  server_busy: "dashboard.lifecycle.busy",
};

// 503 service-unavailable reasons (issue #1092): post-restart scenarios where
// the Worker or JAR backend is not yet ready. Matches the API's RFC 9457
// `reason` extension member on 503 responses.
const SPECIFIC_503_MESSAGE: Record<string, TranslationKey> = {
  no_eligible_worker: "dashboard.lifecycle.noEligibleWorker",
  worker_unavailable: "dashboard.lifecycle.workerUnavailable",
  jar_unavailable: "dashboard.lifecycle.jarUnavailable",
};

export function isEulaNotAccepted(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    error.reason === "eula_not_accepted"
  );
}

export function lifecycleErrorMessage(error: unknown): TranslationKey {
  if (error instanceof ApiError && error.status === 409) {
    if (error.reason !== undefined && error.reason in SPECIFIC_409_MESSAGE) {
      return SPECIFIC_409_MESSAGE[error.reason];
    }
    return "dashboard.stateChanged";
  }
  if (error instanceof ApiError && error.status === 503) {
    if (error.reason !== undefined && error.reason in SPECIFIC_503_MESSAGE) {
      return SPECIFIC_503_MESSAGE[error.reason];
    }
  }
  return "dashboard.actionFailed";
}
