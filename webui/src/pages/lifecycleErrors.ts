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
 * `worker_unavailable` — the API's rendering of a dispatch that timed out or lost
 * the Worker session — is verb-specific on stop and restart for the same reason
 * some 409s are: the intent was already committed, so "try again" is wrong
 * (issue #2440). All other errors fall back to the generic action-failed toast.
 *
 * 403 is intentionally NOT handled here: it carries a side effect (refetching
 * capabilities) that lives in `useOnForbidden`. Callers run that glue first and
 * only reach this helper for non-403 errors.
 *
 * Callers pass the lifecycle verb they asked for. A few reasons leave something
 * pending that depends on it — a failed stop is still going to be retried, a
 * failed restart is still going to come back — and the message says so, on both
 * the 409 dispatch-refused path (issue #2435) and the 503 dispatch-unconfirmed
 * one (issue #2440). The verb is optional, and omitting it just keeps the
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
// an action (issue #2435). The API dispatches AFTER committing the intent, and
// compensation is start-scoped and not even universal there, so what a dispatch
// failure leaves behind differs per verb (servers/application/lifecycle.py):
//
// - start   — compensated back to stopped wherever the start demonstrably did
//             not happen, which is every 409 reason below EXCEPT a post-dispatch
//             BUSY (see the worker_busy note below; a post-dispatch 503
//             `worker_unavailable` is the same carve-out, see
//             VERB_SPECIFIC_503_MESSAGE). So for `command_failed` nothing is
//             pending and the verb-agnostic message is already right. Absent
//             here.
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
// Restart keeps the plain busy message because a BUSY refusal there applied
// nothing and left the server running as it was.
//
// Start is the subtle one, and it keeps the plain busy message DELIBERATELY,
// not by oversight. A pre-dispatch BUSY (a hydrate refused for the same race)
// compensates, so nothing is pending — but a POST-dispatch BUSY does not: the
// Worker refused because another mutating command for this id is already in
// flight with an unknown outcome, so StartServer keeps desired=running plus the
// assignment and lets a later reconcile tick take redispatch_start to the same
// Worker (issue #824, lifecycle.py StartServer.__call__ under
// `dispatch.attempted`). That case DOES leave a start intent pending, and the
// client cannot tell the two apart — both arrive as a bare `worker_busy`.
// Issue #2435 scoped start out on the grounds that "wait and try again" is at
// least not harmful there; whether it deserves its own pending message is
// filed as its own follow-up.
//
// `server_busy` is start-only and never commits an intent, so it is
// verb-independent throughout.
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

// The 503 counterpart of VERB_SPECIFIC_409_MESSAGE, consulted the same way and
// falling through to SPECIFIC_503_MESSAGE (issue #2440). `worker_unavailable` is
// the API's rendering of a dispatch that TIMED OUT or lost the Worker session
// (servers/api/servers.py `_SERVICE_UNAVAILABLE_REASONS`); the generic message
// asks for a retry, which is wrong wherever the verb already committed its
// intent:
//
// - stop    — the only way StopServer can raise it is the `control_plane.stop`
//             call, which happens AFTER desired=stopped is committed and the
//             placement load decremented, and nothing is compensated
//             (lifecycle.py StopServer). So the stop intent is pending on every
//             worker-unavailable stop, with no ambiguity.
// - restart — desired stays running (restart commits no state change), so a
//             Worker that stopped the server and failed to relaunch it leaves
//             the server down with the reconciler about to start it again —
//             the same reasoning as the 409 entry above.
// - start   — absent DELIBERATELY. A PRE-dispatch unavailable (a failed hydrate,
//             or a connect that never reached the Worker) IS compensated back to
//             stopped, so nothing is pending and "try again" is right; a
//             POST-dispatch one keeps desired=running plus the assignment for
//             redispatch_start, so a start IS pending. Both arrive as a bare
//             `worker_unavailable` and the client cannot tell them apart — the
//             same ambiguity #2445 tracks for the BUSY start, so start stays on
//             the verb-agnostic message here too.
//
// The wording is NOT the *Pending pair the 409 entries use. Those failures were
// reported by a Worker that answered, so the toast can say what the server did;
// a timeout answers nothing — a graceful stop simply outliving the API's
// dispatch deadline is the commonest case, and it usually succeeds — so these
// say the outcome is unconfirmed. What survives the failure differs by verb, and
// the strings differ with it: the stop INTENT is re-driven (redispatch_stop
// keeps retrying it), so that string states the retry; restart keeps only
// desired=running — an undelivered restart is never re-sent, and the reconciler
// starts the server only if it does end up down — so that string is conditional
// ("if it stays down") rather than a promise to bring it back.
//
// `no_eligible_worker` and `jar_unavailable` are absent: both are raised by
// StartServer before any intent is committed, so nothing is ever pending.
const VERB_SPECIFIC_503_MESSAGE: Record<
  string,
  Partial<Record<LifecycleAction, TranslationKey>>
> = {
  worker_unavailable: {
    stop: "dashboard.lifecycle.stopUnconfirmed",
    restart: "dashboard.lifecycle.restartUnconfirmed",
  },
};

// Look up the verb-specific override for a reason, if the caller supplied a verb
// and the table has an entry for that verb. Shared by the 409 and 503 paths so
// both mechanisms behave identically: a hit wins, a miss falls through to the
// verb-agnostic table.
function verbSpecificMessage(
  table: Record<string, Partial<Record<LifecycleAction, TranslationKey>>>,
  reason: string,
  action: LifecycleAction | undefined,
): TranslationKey | undefined {
  return action === undefined ? undefined : table[reason]?.[action];
}

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
      const byVerb = verbSpecificMessage(
        VERB_SPECIFIC_409_MESSAGE,
        error.reason,
        action,
      );
      if (byVerb !== undefined) {
        return byVerb;
      }
      if (error.reason in SPECIFIC_409_MESSAGE) {
        return SPECIFIC_409_MESSAGE[error.reason];
      }
    }
    return "dashboard.stateChanged";
  }
  if (error instanceof ApiError && error.status === 503) {
    if (error.reason !== undefined) {
      const byVerb = verbSpecificMessage(
        VERB_SPECIFIC_503_MESSAGE,
        error.reason,
        action,
      );
      if (byVerb !== undefined) {
        return byVerb;
      }
      if (error.reason in SPECIFIC_503_MESSAGE) {
        return SPECIFIC_503_MESSAGE[error.reason];
      }
    }
  }
  return "dashboard.actionFailed";
}
