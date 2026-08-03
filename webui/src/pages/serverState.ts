/**
 * Server observed-state presentation (WEBUI_SPEC.md 2.3 / 6.2).
 *
 * The API's `observed_state` is a free-form string; the dashboard maps the
 * known values to a state pill (color-coded class + label + a blink for the
 * transitional states) and decides which lifecycle actions apply. An
 * unrecognised value degrades to the "unknown" (striped) pill, never a crash.
 */

import type { TranslationKey } from "../i18n/index.ts";

/** The observed-state values the API emits (WEBUI_SPEC.md 2.3). */
export type ObservedState =
  | "starting"
  | "running"
  | "stopping"
  | "stopped"
  | "restarting"
  | "crashed"
  | "unknown";

export const KNOWN: readonly ObservedState[] = [
  "starting",
  "running",
  "stopping",
  "stopped",
  "restarting",
  "crashed",
  "unknown",
];

/** Normalise the API's free-form string to a known state, else "unknown". */
export function normalizeState(state: string): ObservedState {
  return (KNOWN as readonly string[]).includes(state)
    ? (state as ObservedState)
    : "unknown";
}

/** Whether a state is mid-transition (pill blinks; all actions held). */
export function isTransitional(state: ObservedState): boolean {
  return state === "starting" || state === "stopping" || state === "restarting";
}

/**
 * Whether the server is settled at rest — neither running nor transitional.
 * Export, delete and the at-rest settings edits (name/game-port/non-cadence
 * config) gate on this; the API otherwise answers 409 server_unsettled /
 * server_not_stopped (WEBUI_SPEC.md 6.9).
 */
export function atRest(state: ObservedState, desired?: string): boolean {
  if (desired !== undefined && desired !== "stopped") return false;
  return state === "stopped" || state === "crashed" || state === "unknown";
}

/**
 * The four semantic buckets the dashboard state filter groups the raw states
 * into (#2239). `other` folds together the transitional states and `unknown`.
 */
export type StateBucket = "running" | "stopped" | "crashed" | "other";

export const STATE_BUCKETS: readonly StateBucket[] = [
  "running",
  "stopped",
  "crashed",
  "other",
];

/**
 * The bucket an observed state belongs to — the single source of truth for the
 * 4→raw mapping (the dropdown, `filterServers`, and the tests all consume it).
 * `starting` / `stopping` / `restarting` / `unknown` all fall into `other`.
 */
export function bucketOf(state: ObservedState): StateBucket {
  switch (state) {
    case "running":
      return "running";
    case "stopped":
      return "stopped";
    case "crashed":
      return "crashed";
    default:
      return "other";
  }
}

/**
 * The raw observed states a bucket expands to, derived from `bucketOf` so the
 * membership is never duplicated. `other` yields the transitional states plus
 * `unknown`, in `KNOWN` order.
 */
export function statesForBucket(bucket: StateBucket): ObservedState[] {
  return KNOWN.filter((state) => bucketOf(state) === bucket);
}

interface PillSpec {
  /** Pill modifier class (running=green, transition=amber, …). */
  className: string;
  /** Animate the pill dot while the state is settling. */
  blink: boolean;
  /** i18n key for the pill label. */
  labelKey: TranslationKey;
}

/**
 * The pill presentation for an observed state. Colors track the spec: running
 * green, starting/stopping/restarting amber, crashed red, stopped gray,
 * unknown striped (WEBUI_SPEC.md 6.2).
 */
export function statePill(state: ObservedState): PillSpec {
  const labelKey = `dashboard.state.${state}` as TranslationKey;
  switch (state) {
    case "running":
      return { className: "running", blink: false, labelKey };
    case "starting":
    case "stopping":
    case "restarting":
      return { className: state, blink: true, labelKey };
    case "crashed":
      return { className: "crashed", blink: false, labelKey };
    case "stopped":
      return { className: "stopped", blink: false, labelKey };
    case "unknown":
      return { className: "unknown", blink: false, labelKey };
  }
}

/**
 * Whether a lifecycle action applies to the current observed state. While
 * transitional, nothing applies; otherwise start needs an at-rest/crashed
 * server, and stop/restart need a running one.
 */
export function actionApplies(
  action: "start" | "stop" | "restart",
  state: ObservedState,
  desired?: string,
): boolean {
  if (isTransitional(state)) {
    return false;
  }
  if (action === "start") {
    if (desired === "running") return false;
    return state === "stopped" || state === "crashed" || state === "unknown";
  }
  // stop / restart
  if (desired === "running" && (state === "crashed" || state === "unknown")) {
    return true;
  }
  return state === "running";
}
