/** Unknown observed states use the unknown pill instead of failing rendering. */

import type { TranslationKey } from "../i18n/index.ts";

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

export function normalizeState(state: string): ObservedState {
  return (KNOWN as readonly string[]).includes(state)
    ? (state as ObservedState)
    : "unknown";
}

export function isTransitional(state: ObservedState): boolean {
  return state === "starting" || state === "stopping" || state === "restarting";
}

/** At-rest edits, export, and delete require a server that is neither running nor transitioning. */
export function atRest(state: ObservedState, desired?: string): boolean {
  if (desired !== undefined && desired !== "stopped") return false;
  return state === "stopped" || state === "crashed" || state === "unknown";
}

export type StateBucket = "running" | "stopped" | "crashed" | "other";

export const STATE_BUCKETS: readonly StateBucket[] = [
  "running",
  "stopped",
  "crashed",
  "other",
];

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

export function statesForBucket(bucket: StateBucket): ObservedState[] {
  return KNOWN.filter((state) => bucketOf(state) === bucket);
}

interface PillSpec {
  className: string;
  blink: boolean;
  labelKey: TranslationKey;
}

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

/** Hold actions during transitions; running intent also permits stop/restart when observed crashed or unknown. */
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
  if (desired === "running" && (state === "crashed" || state === "unknown")) {
    return true;
  }
  return state === "running";
}
