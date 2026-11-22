// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client.ts";
import { en } from "../i18n/en.ts";
import { ja } from "../i18n/ja.ts";
import {
  lifecycleErrorMessage,
  releaseErrorMessage,
} from "./lifecycleErrors.ts";

describe("lifecycleErrorMessage", () => {
  it("maps a 409 port_conflict to its specific message", () => {
    const error = new ApiError(409, { reason: "port_conflict" });
    expect(lifecycleErrorMessage(error)).toBe(
      "dashboard.lifecycle.portConflict",
    );
  });

  it("maps a 409 image_missing to its specific message", () => {
    const error = new ApiError(409, { reason: "image_missing" });
    expect(lifecycleErrorMessage(error)).toBe(
      "dashboard.lifecycle.imageMissing",
    );
  });

  // server.properties is a symlink, a special file or over the Worker's 4 MiB
  // read cap (issue #3347). Both refusals arrive as this one reason, and the
  // message is the same on start and on restart (whose relaunch meets it).
  it.each([undefined, "start", "restart"] as const)(
    "maps a 409 server_properties_unreadable to its message (action %s)",
    (action) => {
      const error = new ApiError(409, {
        reason: "server_properties_unreadable",
      });
      expect(lifecycleErrorMessage(error, action)).toBe(
        "dashboard.lifecycle.serverPropertiesUnreadable",
      );
    },
  );

  // The command waited on the host past its budget and was not run (issue
  // #3305). Nothing was half-done, so the dispatch-failure message does not
  // apply. A start or restart leaves nothing pending, so restartPending would be
  // wrong. A stop keeps its intent and is replayed after a short grace, but a
  // retry replays it at once, so it keeps "try again" over stopPending.
  it.each([undefined, "start", "stop", "restart"] as const)(
    "maps a 409 command_expired to the not-applied message (action %s)",
    (action) => {
      const error = new ApiError(409, { reason: "command_expired" });
      expect(lifecycleErrorMessage(error, action)).toBe(
        "dashboard.lifecycle.commandExpired",
      );
    },
  );

  it("maps a 409 server_unsettled to the at-rest precondition message", () => {
    const error = new ApiError(409, { reason: "server_unsettled" });
    expect(lifecycleErrorMessage(error)).toBe("serverDetail.error.unsettled");
  });

  // Contention, not a race: the request was refused without being applied and
  // clears on its own (issue #2400).
  it.each(["worker_busy", "server_busy"])(
    "maps a 409 %s to the busy message",
    (reason) => {
      const error = new ApiError(409, { reason });
      expect(lifecycleErrorMessage(error)).toBe("dashboard.lifecycle.busy");
    },
  );

  // The dispatch reached the Worker and was not applied as asked; whether the
  // server moved depends on the verb, so the toast names the failure rather
  // than claiming a state change (issue #2420).
  it("maps a 409 command_failed to the dispatch-failure message", () => {
    const error = new ApiError(409, { reason: "command_failed" });
    expect(lifecycleErrorMessage(error)).toBe(
      "dashboard.lifecycle.commandFailed",
    );
  });

  // A failed start is compensated back to stopped, so nothing is pending and
  // the verb-agnostic message is already honest (issue #2435).
  it("keeps the dispatch-failure message for a failed start", () => {
    const error = new ApiError(409, { reason: "command_failed" });
    expect(lifecycleErrorMessage(error, "start")).toBe(
      "dashboard.lifecycle.commandFailed",
    );
  });

  // A failed stop leaves desired_state=stopped committed over a still-running
  // process, so the stop intent is pending and the reconciler retries it. Both
  // reasons the API can render for it say so (issue #2435).
  it.each(["command_failed", "worker_busy"])(
    "maps a 409 %s on stop to the pending-stop message",
    (reason) => {
      const error = new ApiError(409, { reason });
      expect(lifecycleErrorMessage(error, "stop")).toBe(
        "dashboard.lifecycle.stopPending",
      );
    },
  );

  // A failed restart leaves desired_state=running, so a server the Worker took
  // down is brought back automatically (issue #2435).
  it("maps a 409 command_failed on restart to the pending-restart message", () => {
    const error = new ApiError(409, { reason: "command_failed" });
    expect(lifecycleErrorMessage(error, "restart")).toBe(
      "dashboard.lifecycle.restartPending",
    );
  });

  // Restart is the one verb for which server_not_running is not a race: the
  // dashboard deliberately offers restart for a server observed unknown that is
  // still desired-running, so "state changed" names nothing that changed (issue
  // #2441). What is left pending is what a failed restart always leaves —
  // desired_state=running — so it shares that message.
  it("maps a 409 server_not_running on restart to the pending-restart message", () => {
    const error = new ApiError(409, { reason: "server_not_running" });
    expect(lifecycleErrorMessage(error, "restart")).toBe(
      "dashboard.lifecycle.restartPending",
    );
  });

  // The Worker refused because it could not confirm an earlier stop, so the
  // server's process is probably still running — nothing is pending, nothing
  // moved, and the remedy is a fresh stop. Verb-agnostic: the message names the
  // server's condition, not what the refused verb would have done (issue #2466).
  it.each([undefined, "restart"] as const)(
    "maps a 409 failed_stop_orphan on %s to the orphan message",
    (action) => {
      const error = new ApiError(409, { reason: "failed_stop_orphan" });
      expect(lifecycleErrorMessage(error, action)).toBe(
        "dashboard.lifecycle.failedStopOrphan",
      );
    },
  );

  // worker_busy on restart applied nothing at all — the server keeps running —
  // so it stays on the retry-in-a-moment message (issue #2435).
  it("keeps the busy message for a 409 worker_busy on restart", () => {
    const error = new ApiError(409, { reason: "worker_busy" });
    expect(lifecycleErrorMessage(error, "restart")).toBe(
      "dashboard.lifecycle.busy",
    );
  });

  // A start refused with worker_busy (issue #2445): a POST-dispatch BUSY keeps
  // desired_state=running so the reconciler's redispatch_start applies the start
  // later, and since issue #3890 a PRE-dispatch BUSY (a refused hydrate) does
  // too. The message does not promise the start will be applied, and it drops
  // the "try again" the generic busy message pushes, which on the pending path
  // lands on invalid_transition (see below).
  it("maps a 409 worker_busy on start to the start-pending message", () => {
    const error = new ApiError(409, { reason: "worker_busy" });
    expect(lifecycleErrorMessage(error, "start")).toBe(
      "dashboard.lifecycle.startPending",
    );
  });

  // A start that did not hydrate its Worker while that Worker's upload of the
  // server was in flight keeps desired_state=running and its Worker, and the
  // reconciler retries it (issue #3950): the start is pending.
  it("maps a 409 upload_in_flight on start to the start-pending message", () => {
    const error = new ApiError(409, { reason: "upload_in_flight" });
    expect(lifecycleErrorMessage(error, "start")).toBe(
      "dashboard.lifecycle.startPending",
    );
  });

  // server_busy is start-only and never commits an intent, so the verb never
  // changes its message (issue #2435).
  it("keeps the busy message for a 409 server_busy on start", () => {
    const error = new ApiError(409, { reason: "server_busy" });
    expect(lifecycleErrorMessage(error, "start")).toBe(
      "dashboard.lifecycle.busy",
    );
  });

  it.each(["invalid_transition", "transition_conflict", "server_not_running"])(
    "keeps the state-changed treatment for an unmapped 409 reason (%s)",
    (reason) => {
      const error = new ApiError(409, { reason });
      expect(lifecycleErrorMessage(error)).toBe("dashboard.stateChanged");
    },
  );

  // Retrying a start while desired_state=running — the state the pending-start
  // case above leaves behind — raises invalid_transition. The generic
  // state-changed toast contradicts having just been told the start is pending,
  // so start alone gets a message that says the server is already coming up
  // (issue #2445).
  it("maps a 409 invalid_transition on start to the already-running message", () => {
    const error = new ApiError(409, { reason: "invalid_transition" });
    expect(lifecycleErrorMessage(error, "start")).toBe(
      "dashboard.lifecycle.startAlreadyRunning",
    );
  });

  // Only start leaves a pending intent a retry collides with; stop and restart
  // keep the state-changed treatment for invalid_transition (issue #2445).
  it.each(["stop", "restart"] as const)(
    "keeps the state-changed treatment for a 409 invalid_transition on %s",
    (action) => {
      const error = new ApiError(409, { reason: "invalid_transition" });
      expect(lifecycleErrorMessage(error, action)).toBe(
        "dashboard.stateChanged",
      );
    },
  );

  it("treats a 409 with no reason as state-changed", () => {
    const error = new ApiError(409, undefined);
    expect(lifecycleErrorMessage(error)).toBe("dashboard.stateChanged");
  });

  it("falls back to the generic message for non-409 errors", () => {
    const error = new ApiError(500, { reason: "internal_error" });
    expect(lifecycleErrorMessage(error)).toBe("dashboard.actionFailed");
  });

  it("falls back to the generic message for a non-ApiError", () => {
    expect(lifecycleErrorMessage(new Error("boom"))).toBe(
      "dashboard.actionFailed",
    );
  });

  it("does not treat a port_conflict reason on a non-409 status as specific", () => {
    const error = new ApiError(503, { reason: "port_conflict" });
    expect(lifecycleErrorMessage(error)).toBe("dashboard.actionFailed");
  });

  // 503 service-unavailable reasons (issue #1092).
  it("maps a 503 no_eligible_worker to its specific message", () => {
    const error = new ApiError(503, { reason: "no_eligible_worker" });
    expect(lifecycleErrorMessage(error)).toBe(
      "dashboard.lifecycle.noEligibleWorker",
    );
  });

  it("maps a 503 worker_unavailable to its specific message", () => {
    const error = new ApiError(503, { reason: "worker_unavailable" });
    expect(lifecycleErrorMessage(error)).toBe(
      "dashboard.lifecycle.workerUnavailable",
    );
  });

  // A stop dispatch that timed out or lost its session failed AFTER the API
  // committed desired_state=stopped, so the stop intent stands and the
  // reconciler redispatches it — but the outcome is unknown, so the message
  // can only say it is unconfirmed (issue #2440).
  it("maps a 503 worker_unavailable on stop to the unconfirmed-stop message", () => {
    const error = new ApiError(503, { reason: "worker_unavailable" });
    expect(lifecycleErrorMessage(error, "stop")).toBe(
      "dashboard.lifecycle.stopUnconfirmed",
    );
  });

  // A restart dispatch that could not be confirmed leaves desired_state=running,
  // so a server the Worker took down and failed to relaunch comes back on its
  // own (issue #2440).
  it("maps a 503 worker_unavailable on restart to the unconfirmed-restart message", () => {
    const error = new ApiError(503, { reason: "worker_unavailable" });
    expect(lifecycleErrorMessage(error, "restart")).toBe(
      "dashboard.lifecycle.restartUnconfirmed",
    );
  });

  // A Worker that is not connected gets no automatic recovery: the reconciler
  // skips its servers until it returns, so the server stays where it is until
  // the Worker comes back or the operator moves it (issue #3820).
  it("maps a 503 worker_unavailable from an offline Worker on stop to the offline-stop message", () => {
    const error = new ApiError(503, {
      reason: "worker_unavailable",
      worker_state: "not_connected",
    });
    expect(lifecycleErrorMessage(error, "stop")).toBe(
      "dashboard.lifecycle.stopWorkerOffline",
    );
  });

  it("maps a 503 worker_unavailable from an offline Worker on restart to the offline-restart message", () => {
    const error = new ApiError(503, {
      reason: "worker_unavailable",
      worker_state: "not_connected",
    });
    expect(lifecycleErrorMessage(error, "restart")).toBe(
      "dashboard.lifecycle.restartWorkerOffline",
    );
  });

  // A Worker that did not answer in time is connected and may still finish the
  // command, so the unconfirmed messages stay (issue #3820).
  it.each([
    ["stop", "dashboard.lifecycle.stopUnconfirmed"],
    ["restart", "dashboard.lifecycle.restartUnconfirmed"],
  ] as const)(
    "keeps the unconfirmed message for a 503 worker_unavailable no_answer on %s",
    (action, expected) => {
      const error = new ApiError(503, {
        reason: "worker_unavailable",
        worker_state: "no_answer",
      });
      expect(lifecycleErrorMessage(error, action)).toBe(expected);
    },
  );

  // What the copy promises is the contract for these messages, so each claim
  // is pinned in both locales (issue #3820):
  // - an offline stop completes on a host back in time, with the final
  //   snapshot, and a retry once it is back does that at once (issue #3983);
  //   a stop again while it is away releases the server without that snapshot,
  //   and the release stops nothing on the host;
  // - an offline restart is never re-sent;
  // - moving the server is safe only once the old host is down, since nothing
  //   fences an instance still alive there (CONFIGURATION.md 5.6);
  // - the reconciler restarts a server that stays down only while its Worker
  //   is connected.
  const dictionaries = { en, ja };
  it.each([
    [
      "en",
      "dashboard.lifecycle.stopWorkerOffline",
      /^The server host is not reachable\./,
    ],
    [
      "en",
      "dashboard.lifecycle.stopWorkerOffline",
      /if the host comes back in time, the stop completes there with a final snapshot, and stopping again once it is back does that at once/,
    ],
    [
      "en",
      "dashboard.lifecycle.stopWorkerOffline",
      /stop the server again to release it now, without that snapshot, then start it to move it to another host/,
    ],
    [
      "ja",
      "dashboard.lifecycle.stopWorkerOffline",
      /^サーバーホストに接続できません。/,
    ],
    [
      "ja",
      "dashboard.lifecycle.stopWorkerOffline",
      /猶予時間内にホストが戻れば、そのホストで最終スナップショットを取って停止が完了します。ホストが戻ったあとでもう一度停止すれば、すぐに完了します/,
    ],
    [
      "ja",
      "dashboard.lifecycle.stopWorkerOffline",
      /もう一度停止すると最終スナップショットを取らずにすぐ切り離せます。そのあと起動すると別のホストへ移せます/,
    ],
    [
      "en",
      "dashboard.lifecycle.restartWorkerOffline",
      /^The server host is not reachable, so the system cannot restart the server/,
    ],
    [
      "ja",
      "dashboard.lifecycle.restartWorkerOffline",
      /^サーバーホストに接続できないため、システムはこのサーバーを再起動できません/,
    ],
    [
      "en",
      "dashboard.lifecycle.restartWorkerOffline",
      /Restart it again once the host is back/,
    ],
    [
      "ja",
      "dashboard.lifecycle.restartWorkerOffline",
      /ホストが戻ったら、もう一度再起動してください/,
    ],
    [
      "en",
      "dashboard.lifecycle.stopWorkerOffline",
      /only once you know the old host is down/,
    ],
    [
      "en",
      "dashboard.lifecycle.restartWorkerOffline",
      /only once you know the old host is down/,
    ],
    [
      "ja",
      "dashboard.lifecycle.stopWorkerOffline",
      /元のホストが確実に停止していると分かってから/,
    ],
    [
      "ja",
      "dashboard.lifecycle.restartWorkerOffline",
      /元のホストが確実に停止していると分かってから/,
    ],
    [
      "en",
      "dashboard.lifecycle.restartUnconfirmed",
      /if it stays down, .*as long as the host stays connected/,
    ],
    [
      "ja",
      "dashboard.lifecycle.restartUnconfirmed",
      /停止したままの場合は、ホストとの接続が続いている限り/,
    ],
  ] as const)("%s %s states its condition", (locale, key, claim) => {
    expect(dictionaries[locale][key]).toMatch(claim);
  });

  // The steps and the precondition are inline because a member who can stop a
  // server may have no access to the repository docs (issue #3820).
  it.each([
    ["en", "dashboard.lifecycle.stopWorkerOffline"],
    ["en", "dashboard.lifecycle.restartWorkerOffline"],
    ["ja", "dashboard.lifecycle.stopWorkerOffline"],
    ["ja", "dashboard.lifecycle.restartWorkerOffline"],
  ] as const)("%s %s points to no repository doc", (locale, key) => {
    expect(dictionaries[locale][key]).not.toMatch(/CONFIGURATION\.md/);
  });

  // Start and the verb-agnostic call keep their message for an offline Worker:
  // the offline copy is about a stop or restart intent that stands.
  it.each([undefined, "start"] as const)(
    "keeps the worker-unavailable message for an offline Worker on %s",
    (action) => {
      const error = new ApiError(503, {
        reason: "worker_unavailable",
        worker_state: "not_connected",
      });
      expect(lifecycleErrorMessage(error, action)).toBe(
        "dashboard.lifecycle.workerUnavailable",
      );
    },
  );

  // Start is ambiguous: a pre-dispatch unavailable compensates back to stopped
  // (nothing pending), a post-dispatch one does not, and both arrive as a bare
  // worker_unavailable — so it keeps the verb-agnostic message (issue #2440).
  it("keeps the worker-unavailable message for a failed start", () => {
    const error = new ApiError(503, { reason: "worker_unavailable" });
    expect(lifecycleErrorMessage(error, "start")).toBe(
      "dashboard.lifecycle.workerUnavailable",
    );
  });

  // no_eligible_worker is raised before any intent is committed, on every verb
  // that can see it, so it never becomes verb-specific (issue #2440).
  it("keeps the no-eligible-worker message on stop", () => {
    const error = new ApiError(503, { reason: "no_eligible_worker" });
    expect(lifecycleErrorMessage(error, "stop")).toBe(
      "dashboard.lifecycle.noEligibleWorker",
    );
  });

  it("maps a 503 jar_unavailable to its specific message", () => {
    const error = new ApiError(503, { reason: "jar_unavailable" });
    expect(lifecycleErrorMessage(error)).toBe(
      "dashboard.lifecycle.jarUnavailable",
    );
  });

  it.each(["start", "stop", "restart"] as const)(
    "maps a 503 database_unavailable on %s to the database message (#4010)",
    (action) => {
      const error = new ApiError(503, { reason: "database_unavailable" });
      expect(lifecycleErrorMessage(error, action)).toBe(
        "common.databaseUnavailable",
      );
    },
  );

  it("falls back to the generic message for a 503 with an unknown reason", () => {
    const error = new ApiError(503, { reason: "something_else" });
    expect(lifecycleErrorMessage(error)).toBe("dashboard.actionFailed");
  });

  it("falls back to the generic message for a 503 with no reason", () => {
    const error = new ApiError(503, undefined);
    expect(lifecycleErrorMessage(error)).toBe("dashboard.actionFailed");
  });

  // A stop of a held server retried its final snapshot and the Worker
  // refused it again (#4056): no race, so not the state-changed toast.
  it.each([undefined, "stop"] as const)(
    "maps a 409 final_snapshot_refused to its message (action %s)",
    (action) => {
      const error = new ApiError(409, { reason: "final_snapshot_refused" });
      expect(lifecycleErrorMessage(error, action, "busy")).toBe(
        "dashboard.lifecycle.finalSnapshotRefused",
      );
    },
  );

  // A stop of a server held by a refused final snapshot whose Worker is not
  // connected releases nothing (#4111), so "stop again to release it now" is
  // wrong for it (#4057). A stop that was never delivered keeps that copy.
  describe("a held server's offline stop", () => {
    const offline = new ApiError(503, {
      reason: "worker_unavailable",
      worker_state: "not_connected",
    });

    it.each(["busy", "unavailable"] as const)(
      "maps it to the held-offline message when the server is held (%s)",
      (refusal) => {
        expect(lifecycleErrorMessage(offline, "stop", refusal)).toBe(
          "dashboard.lifecycle.stopHeldWorkerOffline",
        );
      },
    );

    // The 503 the API answers a held stop carries a `detail`, but a retry
    // whose session ends answers a bare one and keeps the refusal, so the
    // caller's held state decides, not the body.
    it("keys on the held state, not on the detail member", () => {
      const bare = new ApiError(503, {
        reason: "worker_unavailable",
        worker_state: "not_connected",
      });
      const detailed = new ApiError(503, {
        reason: "worker_unavailable",
        worker_state: "not_connected",
        detail: "The server's Worker refused its final snapshot …",
      });
      expect(lifecycleErrorMessage(bare, "stop", "busy")).toBe(
        "dashboard.lifecycle.stopHeldWorkerOffline",
      );
      expect(lifecycleErrorMessage(detailed, "stop", null)).toBe(
        "dashboard.lifecycle.stopWorkerOffline",
      );
    });

    it("keeps the offline-stop message for a server no refusal holds", () => {
      expect(lifecycleErrorMessage(offline, "stop", null)).toBe(
        "dashboard.lifecycle.stopWorkerOffline",
      );
      expect(lifecycleErrorMessage(offline, "stop")).toBe(
        "dashboard.lifecycle.stopWorkerOffline",
      );
    });

    it("keeps the unconfirmed message for a held server's no_answer", () => {
      const noAnswer = new ApiError(503, {
        reason: "worker_unavailable",
        worker_state: "no_answer",
      });
      expect(lifecycleErrorMessage(noAnswer, "stop", "busy")).toBe(
        "dashboard.lifecycle.stopUnconfirmed",
      );
    });

    // The exits the API's detail names, pinned in both locales: the Worker's
    // return with the snapshot, the full grace (without it), and the release
    // without the snapshot. And that a second stop releases nothing.
    it.each([
      ["en", /stopping again releases nothing/],
      ["en", /when the host comes back and takes the save/],
      [
        "en",
        /without the save once the host has been away for the full reconcile wait/,
      ],
      ["en", /when someone allowed to stop it releases it without saving/],
      ["ja", /もう一度停止しても切り離されません/],
      ["ja", /ホストが戻って最終スナップショットを取ったとき/],
      [
        "ja",
        /再調整の待ち時間（既定で約11分）が過ぎたとき（スナップショットなし）/,
      ],
      ["ja", /停止の権限を持つ人が保存せずに切り離したとき/],
    ] as const)("%s states the exit %s", (locale, claim) => {
      expect(
        dictionaries[locale]["dashboard.lifecycle.stopHeldWorkerOffline"],
      ).toMatch(claim);
    });

    it.each(["en", "ja"] as const)(
      "%s never tells a held server to stop again to release it",
      (locale) => {
        expect(
          dictionaries[locale]["dashboard.lifecycle.stopHeldWorkerOffline"],
        ).not.toMatch(/release it now|すぐ切り離せます/);
      },
    );
  });
});

// The release of a held server without its final snapshot (#4055) answers
// 409 invalid_transition when no refusal holds the server and 409
// transition_conflict while a stop or a final snapshot of it is in flight.
// Neither is the lifecycle race the state-changed toast names (#4057).
describe("releaseErrorMessage", () => {
  it.each([
    ["invalid_transition", "serverDetail.release.error.notHeld"],
    ["transition_conflict", "serverDetail.release.error.inFlight"],
  ] as const)("maps a 409 %s to its message", (reason, key) => {
    expect(releaseErrorMessage(new ApiError(409, { reason }))).toBe(key);
  });

  it("falls back to the lifecycle mapping for anything else", () => {
    expect(
      releaseErrorMessage(
        new ApiError(503, { reason: "database_unavailable" }),
      ),
    ).toBe("common.databaseUnavailable");
    expect(releaseErrorMessage(new ApiError(409, undefined))).toBe(
      "dashboard.stateChanged",
    );
    expect(releaseErrorMessage(new Error("boom"))).toBe(
      "dashboard.actionFailed",
    );
  });
});
