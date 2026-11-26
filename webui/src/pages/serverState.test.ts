// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { describe, expect, it } from "vitest";
import {
  actionApplies,
  atRest,
  atRestWriteRefusal,
  bucketOf,
  converging,
  crashNotRestarted,
  finalSnapshotRefusal,
  heldNoticeKey,
  isTransitional,
  KNOWN,
  normalizeState,
  type ObservedState,
  releaseWithoutSnapshotApplies,
  STATE_BUCKETS,
  type StateBucket,
  serverAssignedKey,
  startRefusal,
  statePill,
  statesForBucket,
} from "./serverState.ts";
import startGuard from "./startGuard.json";

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
  it("stop / restart apply only to a running server", () => {
    expect(actionApplies("stop", "running")).toBe(true);
    expect(actionApplies("restart", "running")).toBe(true);
    expect(actionApplies("stop", "stopped")).toBe(false);
    expect(actionApplies("restart", "crashed")).toBe(false);
  });

  it("neither stop nor restart applies while transitional", () => {
    for (const action of ["stop", "restart"] as const) {
      expect(actionApplies(action, "starting")).toBe(false);
      expect(actionApplies(action, "stopping")).toBe(false);
      expect(actionApplies(action, "restarting")).toBe(false);
    }
  });

  // The Worker holds no live instance for a crashed server, so the API refuses
  // restart (409 server_not_running) and the reconciler restarts the server on
  // its own; stop stays the operator's one manual move (issue #3419).
  it("crash-looping: stop allowed but restart not when desired=running and observed=crashed", () => {
    expect(actionApplies("stop", "crashed", "running")).toBe(true);
    expect(actionApplies("restart", "crashed", "running")).toBe(false);
  });

  it("stop/restart allowed when desired=running and observed=unknown", () => {
    expect(actionApplies("stop", "unknown", "running")).toBe(true);
    expect(actionApplies("restart", "unknown", "running")).toBe(true);
  });

  it("stop/restart for normally running server with desired", () => {
    expect(actionApplies("stop", "running", "running")).toBe(true);
    expect(actionApplies("restart", "running", "running")).toBe(true);
  });

  // Under a stopped intent the API runs the reconciler's convergence for a row
  // still assigned to a Worker, and refuses (409) one with no Worker (#3491).
  it("stop under desired=stopped applies exactly while a worker is assigned", () => {
    for (const state of ["running", "stopped", "crashed", "unknown"] as const) {
      expect(actionApplies("stop", state, "stopped", true)).toBe(true);
      expect(actionApplies("stop", state, "stopped", false)).toBe(false);
    }
    expect(actionApplies("stop", "stopping", "stopped", true)).toBe(false);
  });

  // The API restarts only a row whose desired state is running, and refuses
  // a stopped intent (409 invalid_transition) even while the process still
  // runs, as in a drain's grace window (#3502).
  it("restart does not apply under desired=stopped, assigned or not", () => {
    for (const state of ["running", "stopped", "crashed", "unknown"] as const) {
      expect(actionApplies("restart", state, "stopped", true)).toBe(false);
      expect(actionApplies("restart", state, "stopped", false)).toBe(false);
    }
    expect(actionApplies("restart", "running", "running", true)).toBe(true);
  });

  // Under a running intent no Worker holds yet (the reconciler still owes its
  // placement), the API withdraws the intent on a stop (#3815). Not while the
  // server still reads running: the stopped intent would keep reading running
  // with no Worker to report otherwise, which nothing converges. Nothing
  // applies while transitional, as elsewhere.
  it("stop under desired=running with no worker applies while at rest", () => {
    for (const state of ["stopped", "crashed", "unknown"] as const) {
      expect(actionApplies("stop", state, "running", false)).toBe(true);
    }
    expect(actionApplies("stop", "running", "running", false)).toBe(false);
    expect(actionApplies("stop", "stopped", "running", true)).toBe(false);
    expect(actionApplies("stop", "stopped", "running")).toBe(false);
    expect(actionApplies("stop", "starting", "running", false)).toBe(false);
  });

  // The API also refuses a restart of a row no Worker is assigned to, under a
  // running intent too (409 invalid_transition, #3502).
  it("restart under desired=running applies only while a worker is assigned", () => {
    for (const state of ["running", "unknown"] as const) {
      expect(actionApplies("restart", state, "running", true)).toBe(true);
      expect(actionApplies("restart", state, "running", false)).toBe(false);
    }
  });
});

describe("startRefusal", () => {
  // The API's answer to a start in every (desired, observed, assigned) row:
  // the start route answers the same for each in
  // api/tests/servers/test_lifecycle_endpoints.py (#3860).
  const { rows } = startGuard;

  it("lists every (desired, observed, assigned) row once", () => {
    const row = (desired: string, observed: string, assigned: boolean) =>
      `${desired}/${observed}/${assigned}`;
    const every = ["running", "stopped"].flatMap((desired) =>
      KNOWN.flatMap((observed) =>
        [false, true].map((assigned) => row(desired, observed, assigned)),
      ),
    );

    expect(
      rows.map((r) => row(r.desired, r.observed, r.assigned)).sort(),
    ).toEqual(every.sort());
  });

  it.each(rows)(
    "$desired / $observed / assigned=$assigned answers $reason",
    ({ desired, assigned, reason }) => {
      expect(startRefusal(desired, assigned)).toBe(reason);
    },
  );
});

describe("atRest", () => {
  // The at-rest copy is served here (the API answers 409 server_unsettled /
  // server_not_stopped otherwise); a write also needs the release, below.
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

describe("atRestWriteRefusal", () => {
  // The API's gate on a write to the at-rest data (`Server.is_quiescent()`,
  // issue #3998): at rest and no Worker assigned. Not at rest is named first,
  // as `not_quiescent_error` does: a running server is always assigned, and
  // the release reason would be the wrong remedy for it.
  const atRestStates: readonly ObservedState[] = [
    "stopped",
    "crashed",
    "unknown",
  ];
  const rows = ["running", "stopped"].flatMap((desired) =>
    KNOWN.flatMap((observed) =>
      [false, true].map((assigned) => ({
        desired,
        observed,
        assigned,
        refusal:
          desired !== "stopped" || !atRestStates.includes(observed)
            ? "not_at_rest"
            : assigned
              ? "server_assigned"
              : null,
      })),
    ),
  );

  it.each(rows)(
    "$desired / $observed / assigned=$assigned answers $refusal",
    ({ desired, observed, assigned, refusal }) => {
      expect(atRestWriteRefusal(observed, desired, assigned)).toBe(refusal);
    },
  );

  it("holds a stopped server its Worker still holds, and frees it on the release", () => {
    expect(atRestWriteRefusal("stopped", "stopped", true)).toBe(
      "server_assigned",
    );
    expect(atRestWriteRefusal("stopped", "stopped", false)).toBeNull();
  });
});

describe("finalSnapshotRefusal", () => {
  // The two refusals the API records (#4054); anything else, a missing field
  // included, is no refusal.
  it.each([
    ["busy", "busy"],
    ["unavailable", "unavailable"],
    [null, null],
    [undefined, null],
    ["timeout", null],
  ] as const)("reads %s as %s", (value, refusal) => {
    expect(finalSnapshotRefusal(value)).toBe(refusal);
  });
});

describe("releaseWithoutSnapshotApplies", () => {
  // The precondition of the API's `…/release-without-snapshot` (#4055): at
  // rest under a stopped intent, still assigned, and held by a refusal.
  const atRestStates: readonly ObservedState[] = [
    "stopped",
    "crashed",
    "unknown",
  ];
  const rows = ["running", "stopped"].flatMap((desired) =>
    KNOWN.flatMap((observed) =>
      [false, true].flatMap((assigned) =>
        ([null, "busy", "unavailable"] as const).map((refusal) => ({
          desired,
          observed,
          assigned,
          refusal,
          applies:
            desired === "stopped" &&
            atRestStates.includes(observed) &&
            assigned &&
            refusal !== null,
        })),
      ),
    ),
  );

  it.each(rows)(
    "$desired / $observed / assigned=$assigned / refusal=$refusal answers $applies",
    ({ desired, observed, assigned, refusal, applies }) => {
      expect(
        releaseWithoutSnapshotApplies(observed, desired, assigned, refusal),
      ).toBe(applies);
    },
  );
});

describe("serverAssignedKey", () => {
  // A hold whose refusal is known is worded by it (#4057); one without, a
  // final snapshot not settled yet, keeps the neutral reason (#4003).
  it("names the refusal a held server is held for", () => {
    expect(serverAssignedKey(null)).toBe("common.serverAssigned");
    expect(serverAssignedKey("busy")).toBe("common.serverHeld.busy");
    expect(serverAssignedKey("unavailable")).toBe(
      "common.serverHeld.unavailable",
    );
  });
});

describe("heldNoticeKey", () => {
  it("words the held notice by the refusal", () => {
    expect(heldNoticeKey("busy")).toBe("serverDetail.held.busy");
    expect(heldNoticeKey("unavailable")).toBe("serverDetail.held.unavailable");
  });
});

describe("converging", () => {
  // The "applying…" hint (WEBUI_SPEC.md 6.4): shown until the server has
  // converged on its intent. A stopped intent is reached by any at-rest state
  // (a server that crashed under it is settled, #3285) once no Worker is
  // assigned; while one is, the stop is still finishing (#3818).
  const cases: Array<[string, ObservedState, boolean, boolean]> = [
    ["running", "running", true, false],
    ["running", "starting", true, true],
    ["running", "stopping", true, true],
    ["running", "restarting", true, true],
    ["running", "stopped", true, true],
    ["running", "crashed", true, true],
    ["running", "unknown", true, true],
    ["stopped", "stopped", false, false],
    ["stopped", "crashed", false, false],
    ["stopped", "unknown", false, false],
    ["stopped", "stopped", true, true],
    ["stopped", "crashed", true, true],
    ["stopped", "unknown", true, true],
    ["stopped", "running", true, true],
    ["stopped", "running", false, true],
    ["stopped", "starting", true, true],
    ["stopped", "stopping", true, true],
    ["stopped", "restarting", true, true],
  ];

  it.each(cases)(
    "desired=%s observed=%s assigned=%s -> %s",
    (desired, state, assigned, expected) => {
      expect(converging(state, desired, assigned)).toBe(expected);
    },
  );
});

describe("crashNotRestarted", () => {
  // A crash the reconciler leaves alone (#3281): it holds only while the
  // server reads crashed under a running intent. The cached flag can outlive
  // both: a Worker disconnect sends `unknown` without touching it, and a live
  // crashed frame sets it from the reason alone, so an optimistic stop or a
  // crash after the stop commit leaves it set under a stopped intent.
  const cases: Array<[ObservedState, string, boolean, boolean]> = [
    ["crashed", "running", true, true],
    ["crashed", "running", false, false],
    ["unknown", "running", true, false],
    ["starting", "running", true, false],
    ["crashed", "stopped", true, false],
  ];

  it.each(cases)(
    "observed=%s desired=%s flag=%s -> %s",
    (state, desired, flag, expected) => {
      expect(crashNotRestarted(state, desired, flag)).toBe(expected);
    },
  );
});

describe("bucketOf", () => {
  // The 4 semantic buckets shown in the dashboard state dropdown (#2239).
  const cases: Array<[ObservedState, StateBucket]> = [
    ["running", "running"],
    ["stopped", "stopped"],
    ["crashed", "crashed"],
    ["starting", "other"],
    ["stopping", "other"],
    ["restarting", "other"],
    ["unknown", "other"],
  ];

  it.each(cases)("%s -> %s bucket", (state, bucket) => {
    expect(bucketOf(state)).toBe(bucket);
  });
});

describe("statesForBucket", () => {
  it("running / stopped / crashed each expand to their single raw state", () => {
    expect(statesForBucket("running")).toEqual(["running"]);
    expect(statesForBucket("stopped")).toEqual(["stopped"]);
    expect(statesForBucket("crashed")).toEqual(["crashed"]);
  });

  it("other folds in the transitional states and unknown", () => {
    expect(statesForBucket("other")).toEqual([
      "starting",
      "stopping",
      "restarting",
      "unknown",
    ]);
  });

  it("every known state maps back into exactly the bucket it expands from", () => {
    for (const bucket of STATE_BUCKETS) {
      for (const state of statesForBucket(bucket)) {
        expect(bucketOf(state)).toBe(bucket);
      }
    }
  });
});
