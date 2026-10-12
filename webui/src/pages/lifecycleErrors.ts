/** Share lifecycle messages across dashboard and detail controls; callers handle 403 capability refresh separately. */

import { ApiError } from "../api/client.ts";
import type { TranslationKey } from "../i18n/index.ts";

export type LifecycleAction = "start" | "stop" | "restart";

// Use specific messages for non-race refusals; other 409s retain the state-changed fallback.
// A failed-stop orphan may still be running and is already being stopped by the host.
const SPECIFIC_409_MESSAGE: Record<string, TranslationKey> = {
  port_conflict: "dashboard.lifecycle.portConflict",
  image_missing: "dashboard.lifecycle.imageMissing",
  server_unsettled: "serverDetail.error.unsettled",
  worker_busy: "dashboard.lifecycle.busy",
  server_busy: "dashboard.lifecycle.busy",
  command_failed: "dashboard.lifecycle.commandFailed",
  failed_stop_orphan: "dashboard.lifecycle.failedStopOrphan",
};

// Stop failures leave a durable stop intent; restart failures retain desired=running for recovery if down.
// Start worker_busy can precede or follow dispatch, so its message must not promise eventual startup.
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

const SPECIFIC_503_MESSAGE: Record<string, TranslationKey> = {
  no_eligible_worker: "dashboard.lifecycle.noEligibleWorker",
  worker_unavailable: "dashboard.lifecycle.workerUnavailable",
  jar_unavailable: "dashboard.lifecycle.jarUnavailable",
};

// Timeouts leave outcome unconfirmed: stop intent is retried, while restart recovers only if down.
// Start can fail before or after intent is retained, so keep its verb-agnostic message.
const VERB_SPECIFIC_503_MESSAGE: Record<
  string,
  Partial<Record<LifecycleAction, TranslationKey>>
> = {
  worker_unavailable: {
    stop: "dashboard.lifecycle.stopUnconfirmed",
    restart: "dashboard.lifecycle.restartUnconfirmed",
  },
};

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
