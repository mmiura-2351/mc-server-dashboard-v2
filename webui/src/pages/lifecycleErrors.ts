/**
 * Map a lifecycle mutation error to its toast message (WEBUI_SPEC.md 7.4).
 *
 * The API returns 409 for lifecycle races (SPEC 7.4, "state changed —
 * refresh") and for several causes that are not races at all. A non-race whose
 * cause is worth naming gets its own message instead of the misleading generic
 * state-changed toast: the sanitized start/restart-failure categories
 * `port_conflict` / `image_missing` (issue #225), `server_unsettled` — the
 * at-rest precondition on the export mint (issue #2360) — the two contention
 * reasons `worker_busy` / `server_busy` (issue #2400), and the unclassified
 * dispatch failure `command_failed` (issue #2420).
 *
 * Three reasons keep the state-changed treatment, and all three are genuine
 * races: `invalid_transition`, `transition_conflict` and `server_not_running`
 * each mean the server really did move, or was never in the state the caller
 * assumed, so refreshing is the right response.
 *
 * 503 responses with a recognized `reason` (`no_eligible_worker`,
 * `worker_unavailable`, `jar_unavailable`) get their own message (issue #1092).
 * All other errors fall back to the generic action-failed toast.
 *
 * 403 is intentionally NOT handled here: it carries a side effect (refetching
 * capabilities) that lives in `useOnForbidden`. Callers run that glue first and
 * only reach this helper for non-403 errors.
 *
 * Callers pass the lifecycle verb they asked for. A few 409 reasons leave
 * something pending that depends on it — a failed stop is still going to be
 * retried, a failed restart is still going to come back — and the message says
 * so (issue #2435). The verb is optional, and omitting it just keeps the
 * verb-agnostic message.
 *
 * Returns a `TranslationKey` so both the dashboard quick actions and the
 * server-detail lifecycle controls (#378 Phase 4) share one mapping.
 */

import { ApiError } from "../api/client.ts";
import type { TranslationKey } from "../i18n/index.ts";

// The lifecycle verb the failed request asked for. Lives here because the
// message for some reasons depends on it (see VERB_SPECIFIC_409_MESSAGE); both
// pages import it rather than each declaring their own.
export type LifecycleAction = "start" | "stop" | "restart";

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
// `plugins.error.busy`); the lifecycle surfaces were the outlier. Stop is the
// one exception, since it commits its intent before the Worker can refuse — see
// VERB_SPECIFIC_409_MESSAGE.
//
// `command_failed` is the catch-all the API renders when a `CommandDispatchError`
// carries no sanitized reason (servers/api/servers.py) — an outcome the Worker
// did not classify, typically INTERNAL. It is NOT a race, so the state-changed
// toast was simply wrong (issue #2420), but neither is it a promise that nothing
// moved: only a failed start is compensated back to stopped. Without a verb the
// message can only name the failure and send the operator to look at the
// server's state; with one it says what is actually pending, which is what
// VERB_SPECIFIC_409_MESSAGE below is for.
//
// The refetch is deliberately kept (issue #2420): it is not a side effect of
// this mapping — every lifecycle mutation invalidates in `onSettled` regardless
// of outcome (DashboardPage.tsx / ServerDetailPage.tsx) — and for the stop and
// restart cases above the client's view really is stale, so dropping it for
// this reason would leave the toast pointing at a state the UI is not showing.
const SPECIFIC_409_MESSAGE: Record<string, TranslationKey> = {
  port_conflict: "dashboard.lifecycle.portConflict",
  image_missing: "dashboard.lifecycle.imageMissing",
  server_unsettled: "serverDetail.error.unsettled",
  worker_busy: "dashboard.lifecycle.busy",
  server_busy: "dashboard.lifecycle.busy",
  command_failed: "dashboard.lifecycle.commandFailed",
};

// Reasons whose honest message depends on the verb, consulted before
// SPECIFIC_409_MESSAGE and falling through to it when the caller did not supply
// an action (issue #2435). The API dispatches AFTER committing the intent and
// compensates only on start, so what a dispatch failure leaves behind differs
// per verb (servers/application/lifecycle.py):
//
// - start   — compensated back to stopped, so nothing is pending and the
//             verb-agnostic messages already say the right thing. Absent here.
// - stop    — desired=stopped is committed over a still-running process and no
//             stop failure class proves the stop will not take effect, so the
//             intent stands and the reconciler's redispatch_stop keeps trying.
//             The operator needs to know the server is still up and that the
//             system is on it, not to check state or retry.
// - restart — desired stays running (restart commits no state change), so a
//             Worker that stopped the server and failed to relaunch it
//             (instancemanager.go handleRestart does not recover one) leaves the
//             server down with the reconciler about to start it again.
//
// `worker_busy` gets the stop treatment for the same reason `command_failed`
// does: StopServer commits and decrements before dispatch, so a BUSY refusal
// still leaves the stop intent durable — "wait and try again" understates it.
// On start and restart nothing was applied and nothing is pending, so those
// keep the plain busy message. `server_busy` is start-only and never commits an
// intent, so it is verb-independent throughout.
const VERB_SPECIFIC_409_MESSAGE: Record<
  string,
  Partial<Record<LifecycleAction, TranslationKey>>
> = {
  command_failed: {
    stop: "dashboard.lifecycle.stopPending",
    restart: "dashboard.lifecycle.restartPending",
  },
  worker_busy: {
    stop: "dashboard.lifecycle.stopPending",
  },
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

export function lifecycleErrorMessage(
  error: unknown,
  action?: LifecycleAction,
): TranslationKey {
  if (error instanceof ApiError && error.status === 409) {
    if (error.reason !== undefined) {
      if (action !== undefined && error.reason in VERB_SPECIFIC_409_MESSAGE) {
        const byVerb = VERB_SPECIFIC_409_MESSAGE[error.reason][action];
        if (byVerb !== undefined) {
          return byVerb;
        }
      }
      if (error.reason in SPECIFIC_409_MESSAGE) {
        return SPECIFIC_409_MESSAGE[error.reason];
      }
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
