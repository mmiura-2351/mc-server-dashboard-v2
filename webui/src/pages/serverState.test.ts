// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { describe, expect, it } from "vitest";
import {
  actionApplies,
  atRest,
  isTransitional,
  normalizeState,
  type ObservedState,
  statePill,
} from "./serverState.ts";

describe("normalizeState", () => {
  it("passes through known states", () => {
    const known: ObservedState[] = [
      "starting",
      "running",
      "stopping",
      "stopped",
      "restarting",
      "crashed",
      "unknown",
    ];
    for (const state of known) {
      expect(normalizeState(state)).toBe(state);
    }
  });

  it("degrades an unrecognised value to unknown", () => {
    expect(normalizeState("bogus")).toBe("unknown");
    expect(normalizeState("")).toBe("unknown");
  });
});

describe("statePill", () => {
  // The full state -> pill-color mapping (WEBUI_SPEC.md 6.2).
  const cases: Array<[ObservedState, string, boolean]> = [
    ["running", "running", false],
    ["starting", "starting", true],
    ["stopping", "stopping", true],
    ["restarting", "restarting", true],
    ["crashed", "crashed", false],
    ["stopped", "stopped", false],
    ["unknown", "unknown", false],
  ];

  it.each(cases)("%s -> .%s (blink=%s)", (state, className, blink) => {
    const pill = statePill(state);
    expect(pill.className).toBe(className);
    expect(pill.blink).toBe(blink);
    expect(pill.labelKey).toBe(`dashboard.state.${state}`);
  });
});

describe("isTransitional", () => {
  it("is true only for the in-flight transition states", () => {
    expect(isTransitional("starting")).toBe(true);
    expect(isTransitional("stopping")).toBe(true);
    expect(isTransitional("restarting")).toBe(true);
    expect(isTransitional("running")).toBe(false);
    expect(isTransitional("stopped")).toBe(false);
    expect(isTransitional("crashed")).toBe(false);
    expect(isTransitional("unknown")).toBe(false);
  });
});

describe("actionApplies", () => {
  it("start applies to at-rest / crashed / unknown servers", () => {
    expect(actionApplies("start", "stopped")).toBe(true);
    expect(actionApplies("start", "crashed")).toBe(true);
    expect(actionApplies("start", "unknown")).toBe(true);
    expect(actionApplies("start", "running")).toBe(false);
  });

  it("stop / restart apply only to a running server", () => {
    expect(actionApplies("stop", "running")).toBe(true);
    expect(actionApplies("restart", "running")).toBe(true);
    expect(actionApplies("stop", "stopped")).toBe(false);
    expect(actionApplies("restart", "crashed")).toBe(false);
  });

  it("nothing applies while transitional", () => {
    for (const action of ["start", "stop", "restart"] as const) {
      expect(actionApplies(action, "starting")).toBe(false);
      expect(actionApplies(action, "stopping")).toBe(false);
      expect(actionApplies(action, "restarting")).toBe(false);
    }
  });

  it("crash-looping: start blocked when desired=running", () => {
    expect(actionApplies("start", "crashed", "running")).toBe(false);
    expect(actionApplies("start", "unknown", "running")).toBe(false);
  });

  it("crash-looping: stop/restart allowed when desired=running and observed=crashed", () => {
    expect(actionApplies("stop", "crashed", "running")).toBe(true);
    expect(actionApplies("restart", "crashed", "running")).toBe(true);
    expect(actionApplies("stop", "unknown", "running")).toBe(true);
    expect(actionApplies("restart", "unknown", "running")).toBe(true);
  });

  it("start allowed when desired=stopped and observed=crashed", () => {
    expect(actionApplies("start", "crashed", "stopped")).toBe(true);
    expect(actionApplies("start", "stopped", "stopped")).toBe(true);
  });

  it("stop/restart for normally running server with desired", () => {
    expect(actionApplies("stop", "running", "running")).toBe(true);
    expect(actionApplies("restart", "running", "running")).toBe(true);
  });
});

describe("atRest", () => {
  // Export / delete / settings edits need the server at rest (the API answers
  // 409 server_unsettled / server_not_stopped otherwise, SPEC 6.9).
  it("is true for the settled states", () => {
    expect(atRest("stopped")).toBe(true);
    expect(atRest("crashed")).toBe(true);
    expect(atRest("unknown")).toBe(true);
  });

  it("is false while running or transitional", () => {
    expect(atRest("running")).toBe(false);
    expect(atRest("starting")).toBe(false);
    expect(atRest("stopping")).toBe(false);
    expect(atRest("restarting")).toBe(false);
  });

  it("crash-looping: not at rest when desired=running", () => {
    expect(atRest("crashed", "running")).toBe(false);
    expect(atRest("unknown", "running")).toBe(false);
    expect(atRest("stopped", "running")).toBe(false);
  });

  it("at rest when desired=stopped and observed is terminal", () => {
    expect(atRest("crashed", "stopped")).toBe(true);
    expect(atRest("stopped", "stopped")).toBe(true);
    expect(atRest("unknown", "stopped")).toBe(true);
  });

  it("running with desired is not at rest", () => {
    expect(atRest("running", "running")).toBe(false);
  });
});
