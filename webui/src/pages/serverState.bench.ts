import { describe, test } from "vitest";
import {
  actionApplies,
  isTransitional,
  normalizeState,
  statePill,
} from "./serverState.ts";

describe("serverState", () => {
  test("normalizeState", async ({ bench }) => {
    await bench("normalizeState", () => {
      normalizeState("running");
      normalizeState("bogus");
    }).run();
  });

  test("statePill", async ({ bench }) => {
    await bench("statePill", () => {
      statePill("running");
      statePill("starting");
      statePill("crashed");
    }).run();
  });

  test("isTransitional + actionApplies", async ({ bench }) => {
    await bench("isTransitional + actionApplies", () => {
      isTransitional("starting");
      isTransitional("running");
      actionApplies("start", "stopped");
      actionApplies("stop", "running");
      actionApplies("restart", "starting");
    }).run();
  });
});
