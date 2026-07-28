/**
 * Map a lifecycle mutation error to its toast message (WEBUI_SPEC.md 7.4).
 *
 * The API returns 409 both for lifecycle races (SPEC 7.4, "state changed —
 * refresh") and for start failures the Worker classified into a sanitized
 * category (issue #225) — e.g. `port_conflict` / `image_missing`. The latter
 * are real, actionable causes, so they get their own message instead of the
 * misleading generic state-changed toast; so is `server_unsettled`, the at-rest
 * precondition on the export mint (issue #2360). Every other 409 reason
 * (`invalid_transition`, `transition_conflict`, `command_failed`,
 * `server_not_running`) is race-flavoured and keeps the state-changed
 * treatment. 503 responses with a recognized `reason` (`no_eligible_worker`,
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

// 409 reasons that get a specific message; everything else 409 stays
// race-flavoured (state changed). `port_conflict` / `image_missing` are the
// sanitized start-failure categories, mirroring the API's `_SANITIZED_REASONS`
// (servers/application/command_dispatch.py). `server_unsettled` is the at-rest
// precondition on the export mint (servers/api/servers.py), which is a standing
// requirement rather than a race, so it names the precondition here too — the
// same message the danger-zone export already shows (issue #2360).
const SPECIFIC_409_MESSAGE: Record<string, TranslationKey> = {
  port_conflict: "dashboard.lifecycle.portConflict",
  image_missing: "dashboard.lifecycle.imageMissing",
  server_unsettled: "serverDetail.error.unsettled",
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
