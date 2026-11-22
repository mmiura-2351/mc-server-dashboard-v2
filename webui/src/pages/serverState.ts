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

/**
 * Whether a state is mid-transition (pill blinks; stop and restart held). Start
 * is not held by it: the API's start guard does not read the observed state
 * (see {@link startRefusal}).
 */
export function isTransitional(state: ObservedState): boolean {
  return state === "starting" || state === "stopping" || state === "restarting";
}

/**
 * Whether the server is settled at rest: under a stopped intent (when
 * `desired` is given), and neither running nor transitional. The API serves
 * the at-rest copy's reads here (a file download), and otherwise answers 409
 * `server_unsettled` / `server_not_stopped`. Being at rest is not enough to
 * write that copy: a stop still holding its Worker refuses the write, so a
 * write control gates on {@link atRestWriteRefusal} instead.
 */
export function atRest(state: ObservedState, desired?: string): boolean {
  if (desired !== undefined && desired !== "stopped") return false;
  return state === "stopped" || state === "crashed" || state === "unknown";
}

/** Why a write to the server's at-rest data is refused now. */
export type AtRestWriteRefusal = "not_at_rest" | "server_assigned";

/**
 * Why the API would refuse a write to the server's at-rest data, or `null`
 * where it accepts one: the one rule the at-rest write controls gate on (the
 * Files, Plugins and Backups restore tabs, the resource pack and the Settings
 * save). It is the API's `Server.is_quiescent()` gate, in the order
 * `not_quiescent_error` names it (issue #3998).
 *
 * - `not_at_rest`: running, transitional or under a running intent
 *   ({@link atRest}). Each surface names its own remedy, stopping the server.
 *   It comes first: a running server is always assigned, and waiting for the
 *   release is not its remedy.
 * - `server_assigned`: at rest, but a Worker is still `assigned`. The stop
 *   holds it until the final snapshot settles, and that snapshot would publish
 *   over the write (409 `server_assigned`). The controls show held, with the
 *   reason, until the release frame frees them (issue #4003).
 * - `null`: at rest and released. The write may be made.
 */
export function atRestWriteRefusal(
  state: ObservedState,
  desired: string,
  assigned: boolean,
): AtRestWriteRefusal | null {
  if (!atRest(state, desired)) return "not_at_rest";
  if (assigned) return "server_assigned";
  return null;
}

/**
 * Whether the server has yet to converge on its intent — the "applying…" hint
 * (WEBUI_SPEC.md 6.4). A running intent is reached only by `running`. A
 * stopped intent is reached by any at-rest state — a server that crashed under
 * it is as settled as a stopped one (#3285) — once no Worker is `assigned`:
 * while one is, the stop is still finishing (its final snapshot and the
 * release, or for an `unknown` server the stop itself) and the API refuses a
 * start. The live status frames carry every release, so the assignment is as
 * fresh as the states (#3818).
 */
export function converging(
  state: ObservedState,
  desired: string,
  assigned: boolean,
): boolean {
  if (atRest(state, desired)) {
    return assigned;
  }
  return state !== desired;
}

/**
 * The crash reasons the reconciler does not restart, as the next attempt fails
 * the same way (CONFIGURATION.md Section 5.6, issue #3281). The API reports
 * its rule as `crash_not_restarted`; a crashed status frame carries only the
 * reason, so the live hooks derive the flag from this set. It mirrors the
 * API's `NOT_RESTARTED_CRASH_REASONS` (`value_objects.py`), which is not in the
 * OpenAPI document; `api/tests/servers/test_entities.py` pins the two together.
 */
export const NOT_RESTARTED_CRASH_REASONS: ReadonlySet<string> = new Set([
  "forge_install_out_of_memory",
  "forge_install_java_incompatible",
]);

/**
 * Whether the server is down on a crash the reconciler leaves alone (issue
 * #3281): crashed under a running intent, with `crash_not_restarted` set.
 * Nothing converges it until the operator stops and starts it, so neither the
 * detail header nor a dashboard row marks it as applying, and the crash banner
 * says it is not restarted automatically.
 */
export function crashNotRestarted(
  state: ObservedState,
  desired: string,
  notRestarted: boolean,
): boolean {
  return state === "crashed" && desired === "running" && notRestarted;
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

/** The 409 reason the API answers a start it refuses. */
export type StartRefusal = "invalid_transition" | "transition_conflict";

/**
 * Why the API would refuse a start, or `null` where it accepts one, by its
 * StartServer entry guard (`servers/application/lifecycle.py`). The guard reads
 * the intent and the assignment, never the observed state, and so does this:
 * the one rule the detail header and the dashboard rows gate Start on (issue
 * #3860).
 *
 * - `invalid_transition`: a running intent. Start is not offered.
 * - `transition_conflict`: a stopped intent whose Worker is still assigned.
 *   The stop has not finished: a drain's grace window, a stop left half-done,
 *   or the final snapshot before the release (issue #3818). Start shows
 *   disabled, with that reason.
 * - `null`: a stopped intent with no Worker assigned. Start is offered whatever
 *   the server reads. A `starting`, `running`, `stopping` or `restarting` there
 *   is one no Worker reports on any more and nothing else converges, so Start
 *   is the way out. If the server is in fact still live on a Worker the row no
 *   longer names, the start can place a second instance on another Worker
 *   (issue #3740).
 *
 * `startGuard.json` lists the API's answer for every (desired, observed,
 * assigned) row; this function and the API's start route are both tested
 * against it.
 */
export function startRefusal(
  desired: string,
  assigned: boolean,
): StartRefusal | null {
  if (desired === "running") return "invalid_transition";
  if (assigned) return "transition_conflict";
  return null;
}

/**
 * Whether stop or restart applies to the current observed state. While
 * transitional, neither applies; otherwise both need a running server. Start
 * has its own rule, {@link startRefusal}.
 *
 * Under a running intent, stop also applies to a crashed or unknown server, and
 * restart to an unknown one. Restart does not apply to a crashed one: the
 * Worker holds no live instance, so the API refuses it (409
 * `server_not_running`) and the reconciler restarts the server on its own
 * (issue #3419). With no Worker `assigned` (a start the reconciler has yet to
 * place) stop applies to a stopped, crashed or unknown server: the API
 * withdraws the intent, with no Worker to tell (issue #3815). Not to a running
 * one: the stopped intent would keep reading running, with no Worker to report
 * otherwise, and nothing converges that.
 *
 * Under a stopped intent, stop applies exactly while a Worker is still
 * `assigned`: the API then runs the convergence the reconciler would (a drain
 * still running, or a stop left half-done), and refuses it with no Worker
 * (issue #3491). Restart never applies there: the API restarts only under a
 * running intent and refuses a stopped one (409 `invalid_transition`), even
 * while the process still runs, as in a drain's grace window. It refuses a
 * restart with no Worker assigned the same way, under either intent
 * (issue #3502).
 */
export function actionApplies(
  action: "stop" | "restart",
  state: ObservedState,
  desired?: string,
  assigned?: boolean,
): boolean {
  if (isTransitional(state)) {
    return false;
  }
  if (action === "stop" && desired === "stopped") {
    return assigned === true;
  }
  if (action === "stop" && desired === "running" && assigned === false) {
    return state === "stopped" || state === "crashed" || state === "unknown";
  }
  if (action === "restart" && (desired === "stopped" || assigned === false)) {
    return false;
  }
  // stop / restart
  if (desired === "running" && state === "unknown") {
    return true;
  }
  if (desired === "running" && state === "crashed") {
    return action === "stop";
  }
  return state === "running";
}
