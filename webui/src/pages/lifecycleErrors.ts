/**
 * Map a lifecycle mutation error to its toast message (WEBUI_SPEC.md 7.4).
 *
 * The API returns 409 for lifecycle races (SPEC 7.4, "state changed —
 * refresh") and for several causes that are not races at all. A non-race whose
 * cause is worth naming gets its own message instead of the misleading generic
 * state-changed toast: the sanitized start/restart-failure categories
 * `port_conflict` / `image_missing` (issue #225), `server_unsettled` — the
 * at-rest precondition on the export mint (issue #2360) — the two contention
 * reasons `worker_busy` / `server_busy` (issue #2400), the unclassified
 * dispatch failure `command_failed` (issue #2420), and `failed_stop_orphan` —
 * a Worker that could not confirm an earlier stop, so the server's process is
 * probably still running (issue #2466).
 *
 * Three reasons keep the state-changed treatment: `invalid_transition`,
 * `transition_conflict` and `server_not_running` each mean the server really
 * did move, or was never in the state the caller assumed, so refreshing is the
 * right response. `server_not_running` is that only away from restart — the
 * dashboard offers restart for a crashed server on purpose, so there the
 * refusal is no race at all and it takes a verb-specific message (issue
 * #2441).
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
// `failed_stop_orphan` is the API's rendering of a Worker that refused because
// it holds a failed-stop orphan for this server: an instance whose stop it could
// not confirm, so the process may still be running (issue #2466). Only restart
// and console reach it — a start, hydrate or snapshot over the same orphan now
// arrives as `worker_busy` instead, because those WILL be carried out once the
// orphan clears while a restart will not (issue #2476). It is deliberately NOT
// verb-specific and deliberately not any of the messages above. Nothing is
// pending (`restartPending` would promise a comeback for a server that never went
// down), nothing changed state (`stateChanged` names no change), and `busy` is
// wrong even though the wait is similar — it says another *operation* is running
// and will finish, not that the server is in a state the host is repairing. What
// IS true is the server's condition plus the fact that the host is already
// converging it on its own (issue #2475), and that holds whichever verb was
// refused, so it lives here rather than in VERB_SPECIFIC_409_MESSAGE.
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
  failed_stop_orphan: "dashboard.lifecycle.failedStopOrphan",
};

// Reasons whose honest message depends on the verb, consulted before
// SPECIFIC_409_MESSAGE and falling through to it when the caller did not supply
// an action — or, for a reason SPECIFIC_409_MESSAGE does not carry, to the
// state-changed toast (issue #2435). The API dispatches AFTER committing the intent, and
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
// Start is the subtle one (issue #2445). It has TWO worker_busy paths that are
// indistinguishable at the edge — both arrive as a bare `worker_busy` because
// the API renders BUSY from the command status alone (command_dispatch.py
// `_SANITIZED_REASONS`):
//
// - POST-dispatch BUSY — the Worker refused the START because another mutating
//   command for this id is already in flight (issue #824) or it holds a
//   failed-stop orphan its converger is resolving (issue #2476). StartServer
//   keeps desired=running plus the assignment and a later reconcile tick takes
//   redispatch_start to the SAME Worker (lifecycle.py StartServer.__call__ under
//   `dispatch.attempted`). The start IS pending.
// - PRE-dispatch BUSY — the HYDRATE that runs before the start was refused for
//   the same two reasons; the start command was never sent, so StartServer
//   compensates back to desired=stopped and unassigns. NOTHING is pending.
//
// So the firm "…will be applied automatically" promise the stop/restart entries
// make would be a FALSE promise here on the pre-dispatch path. #2435 scoped
// start out and kept the plain busy message; this message replaces it with one
// honest for BOTH paths: it says the start MAY still be applied on its own and
// tells the operator to start it again only if the server stays stopped, rather
// than the generic "wait and try again" — which on the pending path lands the
// retry on `invalid_transition` (see the invalid_transition entry below). The
// 503 `worker_unavailable` start path has the identical pre/post ambiguity and
// deliberately stays verb-agnostic (VERB_SPECIFIC_503_MESSAGE); the difference
// is that a timeout answers nothing at all, whereas a BUSY at least tells us the
// host is busy, which is what this message can honestly say.
//
// `invalid_transition` on start is start-only for the same reason: StartServer
// raises it only when desired_state is ALREADY running (lifecycle.py, the entry
// guard), which is exactly the state a pending start leaves behind. An operator
// told the start is pending who clicks start again would otherwise get the
// generic state-changed toast — a second, contradictory answer (issue #2445) —
// so start alone maps it to a message that says the server is already coming up.
// Stop and restart keep the state-changed treatment: neither leaves a pending
// intent a retry collides with this way.
//
// `server_busy` is start-only and never commits an intent, so it is
// verb-independent throughout.
//
// `server_not_running` is the API's rendering of a Worker that holds no live
// instance for the id (lifecycle.py, RestartServer and SendServerCommand). On
// the command surface it genuinely is a race — the caller had just been told
// the server was running — but the dashboard offers restart for a server
// observed crashed or unknown under a running intent ON PURPOSE
// (serverState.ts `actionApplies`), so there the refusal is the expected answer
// and "state changed" names nothing that changed (issue #2441). What it leaves
// behind is what every failed restart leaves: desired=running stands, so the
// reconciler starts the server back up once its observed state reflects that it
// is down (reconciler.py `_NOT_RUNNING` -> redispatch_start). That is exactly
// what restartPending says, so this reuses it rather than adding a second
// string with the same content. Start and stop never see this reason:
// StartServer cannot raise it, and StopServer reads the same Worker outcome as
// convergence and succeeds.
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
    start: "dashboard.lifecycle.startPending",
  },
  invalid_transition: {
    start: "dashboard.lifecycle.startAlreadyRunning",
  },
  server_not_running: {
    restart: "dashboard.lifecycle.restartPending",
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
