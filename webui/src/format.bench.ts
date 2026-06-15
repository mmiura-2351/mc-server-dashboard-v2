/**
 * Benchmark: formatting utilities (issue #1122).
 *
 * Measures the shared formatting helpers used across pages. To add a new
 * format benchmark, add a bench() call inside the describe block.
 */

import { bench, describe } from "vitest";
import { heartbeatAge, humanizeBytes, shortId, statusPill } from "./format.ts";

describe("format", () => {
  bench("humanizeBytes", () => {
    humanizeBytes(1610612736);
  });

  bench("shortId", () => {
    shortId("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee");
  });

  bench("statusPill", () => {
    statusPill("online");
    statusPill("draining");
    statusPill("offline");
  });

  bench("heartbeatAge", () => {
    heartbeatAge(new Date(Date.now() - 45000).toISOString());
  });
});
