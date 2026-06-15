/**
 * Benchmark: server observed-state logic (issue #1122).
 *
 * Measures the state-presentation functions that run on every render of the
 * server detail page. To add a new state benchmark, add a bench() call.
 */

import { bench, describe } from "vitest";
import {
  actionApplies,
  isTransitional,
  normalizeState,
  statePill,
} from "./serverState.ts";

describe("serverState", () => {
  bench("normalizeState", () => {
    normalizeState("running");
    normalizeState("bogus");
  });

  bench("statePill", () => {
    statePill("running");
    statePill("starting");
    statePill("crashed");
  });

  bench("isTransitional + actionApplies", () => {
    isTransitional("starting");
    isTransitional("running");
    actionApplies("start", "stopped");
    actionApplies("stop", "running");
    actionApplies("restart", "starting");
  });
});
