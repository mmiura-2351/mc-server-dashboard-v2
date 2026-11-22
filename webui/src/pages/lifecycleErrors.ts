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
 * dispatch failure `command_failed` (issue #2420), `failed_stop_orphan` —
 * a Worker that could not confirm an earlier stop, so the server's process is
 * probably still running (issue #2466) — `server_properties_unreadable`,
 * a `server.properties` the Worker refuses to read (issue #3347) —
 * `command_expired`, a command the busy Worker did not get to in time (issue
 * #3305) — and `final_snapshot_refused`, a held server's final snapshot that
 * its Worker refused again (issue #4056).
 *
 * Three reasons keep the state-changed treatment: `invalid_transition`,
 * `transition_conflict` and `server_not_running` each mean the server really
 * did move, or was never in the state the caller assumed, so refreshing is the
 * right response. `server_not_running` is that only away from restart — the
 * dashboard offers restart for a server observed unknown under a running intent
 * on purpose, so there the refusal is no race at all and it takes a
 * verb-specific message (issue #2441).
 *
 * 503 responses with a recognized `reason` (`no_eligible_worker`,
 * `worker_unavailable`, `jar_unavailable`) get their own message (issue #1092).
 * `worker_unavailable` — the API's rendering of a dispatch to a Worker that is
 * not connected or did not answer in time, and of a Worker that answered that
 * its container backend did not cooperate (issue #3673) — is verb-specific on
 * stop and restart for the same reason some 409s are: the intent was already
 * committed, so "try again" is wrong (issue #2440). On those two verbs a Worker
 * the API names `not_connected` gets a message of its own (issue #3820). All
 * other errors fall back to the generic action-failed toast.
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
import type { FinalSnapshotRefusal } from "./serverState.ts";

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
// `server_properties_unreadable` is the Worker refusing the server's
// `server.properties` — a symlink, a FIFO or other special file, or a file over
// its 4 MiB read cap — so it could not take the ports for a start or a restart's
// relaunch (issue #3347). The two refusals arrive as this one reason. The remedy
// is the same whatever the verb: restore a regular file while the server is
// stopped. The message suggests a backup restore rather than the Files tab: a
// snapshot leaves a symlink or special file out, so the stored copy then has no
// server.properties at all, and the Files tab refuses to recreate the
// platform-managed keys (rcon.password among them) a backup restore re-applies.
//
// `command_expired` is a command that waited on the Worker past its budget —
// behind an earlier command for the server, or for a slot of the Worker-wide
// concurrency cap — and was answered without being run (issue #3305). What it
// says on every verb is that this Worker command did not execute. It is
// deliberately NOT verb-specific and not `command_failed`'s message: the host
// never ran the command, so nothing was half-done and there is no state to go
// and check. What the API committed before dispatch still differs per verb. A
// start whose start command expired is compensated back to stopped, and a
// restart commits no state change and left the server as it was, so nothing is
// pending on either. A start whose hydrate expired keeps desired=running on its
// Worker for the reconciler instead (issue #3890), so a retry of that one
// answers `invalid_transition` (the start entry below). A stop keeps
// desired=stopped committed over a server that is still running. Its refusal is
// recorded (stop_dispatch_refusals.py), so the reconciler's `redispatch_stop`
// replays the stop on its own after the short refused-stop grace, even past
// the `running` report that usually arrives with the answer. A newer intent
// commit, another observed state or an API restart returns it to the full
// grace (issue #3682). "Try again" still holds there, and it is kept over
// stopPending: an operator stop of that row skips the grace and replays the
// stop at once (StopServer's `redispatch_stop`), whereas the `worker_busy`
// stop that stopPending also covers would only collect another BUSY on a
// retry.
//
// `final_snapshot_refused` is a stop of a server held by a refused final
// snapshot (issue #4056): the stop retried the snapshot and the Worker refused
// it BUSY or UNAVAILABLE again. Only a stop reaches it, and nothing moved: the
// server stays held, the API keeps retrying on its own, and the release
// without the snapshot is the operator's way out, which the message names
// (issue #4057).
//
// The refetch is deliberately kept (issue #2420): it is not a side effect of
// this mapping — every lifecycle mutation invalidates in `onSettled` regardless
// of outcome (useLifecycleMutation.ts) — and for the stop and
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
  server_properties_unreadable:
    "dashboard.lifecycle.serverPropertiesUnreadable",
  command_expired: "dashboard.lifecycle.commandExpired",
  final_snapshot_refused: "dashboard.lifecycle.finalSnapshotRefused",
};

// Reasons whose honest message depends on the verb, consulted before
// SPECIFIC_409_MESSAGE and falling through to it when the caller did not supply
// an action — or, for a reason SPECIFIC_409_MESSAGE does not carry, to the
// state-changed toast (issue #2435). The API dispatches AFTER committing the intent, and
// compensation is start-scoped and not even universal there, so what a dispatch
// failure leaves behind differs per verb (servers/application/lifecycle.py):
//
// - start   — compensated back to stopped wherever the start demonstrably did
//             not happen, which is every 409 reason below EXCEPT a BUSY (see the
//             worker_busy note below) and an expired hydrate (the
//             command_expired note above); a 503 `worker_unavailable` from a
//             post-dispatch timeout or lost session, or from a refused hydrate,
//             is the same carve-out, see VERB_SPECIFIC_503_MESSAGE. So for
//             `command_failed` nothing is pending and the verb-agnostic message
//             is already right. Absent here.
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
//   the same two reasons, so the start command was never sent. Either reason
//   leaves an instance of the server on that Worker possible, so StartServer
//   keeps desired=running plus the assignment here too (issue #3890). The start
//   IS pending.
//
// Until #3890 the pre-dispatch path compensated back to stopped, so the firm
// "…will be applied automatically" promise the stop/restart entries make would
// have been FALSE there. #2435 scoped start out and kept the plain busy
// message; this message replaces it with one honest for BOTH paths: it says
// the start MAY still be applied on its own and
// tells the operator to start it again only if the server stays stopped, rather
// than the generic "wait and try again" — which on the pending path lands the
// retry on `invalid_transition` (see the invalid_transition entry below). The
// 503 `worker_unavailable` start path has the identical pre/post ambiguity and
// deliberately stays verb-agnostic (VERB_SPECIFIC_503_MESSAGE); the difference
// is that a timeout answers nothing at all, and the Worker's own UNAVAILABLE
// says only that its container backend did not cooperate, whereas a BUSY at
// least tells us the host is busy, which is what this message can honestly say.
//
// `invalid_transition` on start is start-only for the same reason: StartServer
// raises it when desired_state is ALREADY running (lifecycle.py, the entry
// guard), which is exactly the state a pending start leaves behind, and when the
// Worker it placed the server on refused the hydrate because it already runs
// the server, which leaves that same state (issue #3865). An operator
// told the start is pending who clicks start again would otherwise get the
// generic state-changed toast — a second, contradictory answer (issue #2445) —
// so start alone maps it to a message that says the server is already coming up.
// Stop and restart keep the state-changed treatment: neither leaves a pending
// intent a retry collides with this way.
//
// `upload_in_flight` is start-only too (issue #3950): the start did not
// hydrate its Worker because a snapshot upload of the server from that Worker
// was still in flight. StartServer keeps desired=running and the assignment,
// as after a refused hydrate, and the reconciler retries the start there. The
// start IS pending, so it gets the start-pending message.
//
// `server_busy` is start-only and never commits an intent, so it is
// verb-independent throughout.
//
// `server_not_running` is the API's rendering of a Worker that holds no live
// instance for the id (lifecycle.py, RestartServer and SendServerCommand). On
// the command surface it genuinely is a race — the caller had just been told
// the server was running — but the dashboard offers restart for a server
// observed unknown under a running intent ON PURPOSE (serverState.ts
// `actionApplies`; no longer for a crashed one, which the API always refuses,
// issue #3419), so there the refusal is the expected answer and "state changed"
// names nothing that changed (issue #2441). A crash that lands just before a
// restart of a running server gets the same answer. What it leaves
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
  upload_in_flight: {
    start: "dashboard.lifecycle.startPending",
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
  // The API's database is down (issue #4010).
  database_unavailable: "common.databaseUnavailable",
};

// The 503 counterpart of VERB_SPECIFIC_409_MESSAGE, consulted the same way and
// falling through to SPECIFIC_503_MESSAGE (issue #2440). `worker_unavailable` is
// the API's rendering of a dispatch to a Worker that is NOT CONNECTED or lost
// its session, of one that TIMED OUT, and of a Worker that answered UNAVAILABLE
// because its container backend did not cooperate (issue #3673;
// servers/api/servers.py `_SERVICE_UNAVAILABLE_REASONS`, command_dispatch.py
// `_SANITIZED_REASONS`); the generic message asks for a retry, which is wrong
// wherever the verb already committed its intent:
//
// - stop    — StopServer raises it from one of two dispatches, both AFTER
//             desired=stopped is committed and the placement load decremented,
//             and nothing is compensated (lifecycle.py StopServer): the
//             `control_plane.stop` call, or, converging a server already
//             desired-stopped (`_clear_stale_assignment`), the final snapshot
//             whose timeout raises WorkerUnavailableError. So the stop intent is
//             pending on every worker-unavailable stop, with no ambiguity.
// - restart — desired stays running (restart commits no state change), so a
//             Worker that stopped the server and failed to relaunch it (its
//             relaunch answered UNAVAILABLE among the rest) leaves the server
//             down with the reconciler about to start it again while that
//             Worker stays connected — the same reasoning as the 409 entry
//             above. A Worker that is not connected gets no such restart; see
//             WORKER_NOT_CONNECTED_503_MESSAGE below.
// - start   — absent DELIBERATELY. A PRE-dispatch unavailable (a failed hydrate,
//             or a connect that never reached the Worker) IS compensated back to
//             stopped, and so is a start the Worker itself answered UNAVAILABLE,
//             so nothing is pending and "try again" is right; a POST-dispatch
//             timeout or lost session, and a hydrate the Worker answered
//             UNAVAILABLE (its leftover container may still run, issue #3890),
//             keep desired=running plus the assignment for redispatch_start, so a
//             start IS pending. All arrive as a bare
//             `worker_unavailable` and the client cannot tell them apart — the
//             `worker_state` member does not split them either, since each state
//             arises both before and after the dispatch. It is the same
//             ambiguity #2445 tracks for the BUSY start, so start stays on the
//             verb-agnostic message here too.
//
// The wording is NOT the *Pending pair the 409 entries use. Those failures were
// reported by a Worker that answered, so the toast can say what the server did;
// a timeout answers nothing — a graceful stop simply outliving the API's
// dispatch deadline is the commonest case, and it usually succeeds — so these
// say the outcome is unconfirmed. A restart whose relaunch the Worker answered
// UNAVAILABLE shares the restart string: that answer says only that the relaunch
// did not complete successfully, and the reconciler starts the server if it
// stays down. What survives the failure differs by verb, and
// the strings differ with it: the stop INTENT is re-driven (redispatch_stop
// keeps retrying it), so that string states the retry; restart keeps only
// desired=running — an undelivered restart is never re-sent, and the reconciler
// starts the server only if it does end up down, and only while its Worker is
// connected — so that string is conditional ("if it stays down", "as long as
// the host stays connected") rather than a promise to bring it back.
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

// Consulted before VERB_SPECIFIC_503_MESSAGE when the API names the Worker
// `not_connected` (the `worker_state` member of a lifecycle 503, issue #3820):
// not connected at dispatch, or its session ended while the command waited (a
// fast reconnect retires the old session's commands the same way, so the Worker
// may in fact be back; the recovery below still holds). The unconfirmed
// messages fit a Worker that is connected and may still finish the command. A
// Worker that is not connected gets no automatic recovery: the reconciler skips
// its servers until it returns, and nothing moves them on its own
// (CONFIGURATION.md 5.6, "A Worker that never reconnects").
//
// - stop    — the intent stands, as above, but nothing is re-sent while the
//             Worker is gone. The reconciler releases the server from it once
//             its grace lapses, or a second stop does at once, and a start then
//             places it on another Worker. A Worker back before the release
//             gets the stop and the final snapshot, which a retried stop takes
//             at once, including for a server whose process is already gone
//             and is owed only that snapshot (issue #3983); one back after it
//             keeps its instance, and nothing stops it.
// - restart — desired stays running, but an undelivered restart is never
//             re-sent, so the operator restarts it once the Worker is back, or
//             moves it (stop, stop again, start).
//
// Both say moving is safe only once the old host is known to be down: the
// release stops nothing there, so an instance still alive on it would run
// beside the new one, and nothing fences it. The copy carries the steps and
// that precondition itself rather than pointing to the docs, which not every
// member can read. Start is absent for the reason given above. `no_answer`, and no member at all (the Worker's own UNAVAILABLE
// answer, or a cause the API cannot name), keep the unconfirmed messages.
const WORKER_NOT_CONNECTED_503_MESSAGE: Record<
  string,
  Partial<Record<LifecycleAction, TranslationKey>>
> = {
  worker_unavailable: {
    stop: "dashboard.lifecycle.stopWorkerOffline",
    restart: "dashboard.lifecycle.restartWorkerOffline",
  },
};

// Consulted before WORKER_NOT_CONNECTED_503_MESSAGE when the caller's server is
// held by a refused final snapshot (issue #4111). The API keeps such a server
// held through a stop while its Worker is gone: the stop releases nothing, so
// "stop again to release it now" is wrong for it. It is released by the
// Worker's return with the snapshot, by the reconciler's full grace without
// it, or by the release without the snapshot, and the message names those
// three (issue #4057).
//
// It keys on the caller's held state or on the response, whichever shows the
// server held. Of the lifecycle 503s, only the API's answer to a held stop
// carries a `detail`, so it shows the server held to a caller that has yet to
// see the refusal. A retry of the held server's snapshot whose session ends
// answers a bare `not_connected` 503 instead and keeps the refusal, so there
// the caller's held state shows it.
const HELD_WORKER_NOT_CONNECTED_503_MESSAGE: Record<
  string,
  Partial<Record<LifecycleAction, TranslationKey>>
> = {
  worker_unavailable: {
    stop: "dashboard.lifecycle.stopHeldWorkerOffline",
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

/**
 * The toast for a failed lifecycle request. `refusal` is the final-snapshot
 * refusal the caller's server is held for (`finalSnapshotRefusal`), when the
 * caller knows it: a held server's offline stop is worded apart, whether the
 * refusal or the 503's `detail` shows it held (issue #4057).
 */
export function lifecycleErrorMessage(
  error: unknown,
  action?: LifecycleAction,
  refusal?: FinalSnapshotRefusal | null,
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
      const held =
        refusal != null || error.detail !== undefined
          ? verbSpecificMessage(
              HELD_WORKER_NOT_CONNECTED_503_MESSAGE,
              error.reason,
              action,
            )
          : undefined;
      const offline =
        error.workerState === "not_connected"
          ? (held ??
            verbSpecificMessage(
              WORKER_NOT_CONNECTED_503_MESSAGE,
              error.reason,
              action,
            ))
          : undefined;
      const byVerb =
        offline ??
        verbSpecificMessage(VERB_SPECIFIC_503_MESSAGE, error.reason, action);
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

// The 409s of the release of a held server without its final snapshot (issue
// #4055). Neither is the lifecycle race the state-changed toast names:
// `invalid_transition` says no refusal holds the server any more (its snapshot
// may have succeeded since, or someone else released it), and
// `transition_conflict` that a stop or a final snapshot of it is still in
// flight, which the release must not cut short (issue #4057).
const RELEASE_409_MESSAGE: Record<string, TranslationKey> = {
  invalid_transition: "serverDetail.release.error.notHeld",
  transition_conflict: "serverDetail.release.error.inFlight",
};

/** The toast for a failed release without the final snapshot. */
export function releaseErrorMessage(error: unknown): TranslationKey {
  if (
    error instanceof ApiError &&
    error.status === 409 &&
    error.reason !== undefined &&
    error.reason in RELEASE_409_MESSAGE
  ) {
    return RELEASE_409_MESSAGE[error.reason];
  }
  return lifecycleErrorMessage(error);
}
