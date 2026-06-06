/**
 * Map a lifecycle mutation error to its toast message (WEBUI_SPEC.md 7.4).
 *
 * The API returns 409 both for lifecycle races (SPEC 7.4, "state changed —
 * refresh") and for start failures the Worker classified into a sanitized
 * category (issue #225) — e.g. `port_conflict` / `image_missing`. The latter
 * are real, actionable causes, so they get their own message instead of the
 * misleading generic state-changed toast. Every other 409 reason
 * (`invalid_transition`, `transition_conflict`, `command_failed`,
 * `server_not_running`) is race-flavoured and keeps the state-changed
 * treatment. Non-409 errors fall back to the generic action-failed toast.
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

// Sanitized 409 start-failure reasons that get a specific message; everything
// else 409 stays race-flavoured (state changed). Mirrors the API's
// `_SANITIZED_REASONS` (servers/application/command_dispatch.py).
const SPECIFIC_409_MESSAGE: Record<string, TranslationKey> = {
  port_conflict: "dashboard.lifecycle.portConflict",
  image_missing: "dashboard.lifecycle.imageMissing",
};

export function lifecycleErrorMessage(error: unknown): TranslationKey {
  if (error instanceof ApiError && error.status === 409) {
    if (error.reason !== undefined && error.reason in SPECIFIC_409_MESSAGE) {
      return SPECIFIC_409_MESSAGE[error.reason];
    }
    return "dashboard.stateChanged";
  }
  return "dashboard.actionFailed";
}
