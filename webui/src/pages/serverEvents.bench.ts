/**
 * Benchmark: server-events frame parsing (issue #1122).
 *
 * Measures parseServerFrame, the JSON parse + type-switch on every inbound
 * WebSocket frame. To add a new data-path benchmark, add a test with an
 * awaited bench().run().
 */

import { describe, test } from "vitest";
import { parseServerFrame } from "./serverEvents.ts";

const STATUS_FRAME = JSON.stringify({
  stream: "status",
  ts: "2026-06-15T00:00:00Z",
  payload: { state: "running", detail: "" },
});

const LOG_FRAME = JSON.stringify({
  stream: "log",
  ts: "2026-06-15T00:00:00Z",
  payload: { line: "[Server] Done (3.456s)!", stream: "stdout" },
});

const METRICS_FRAME = JSON.stringify({
  stream: "metrics",
  ts: "2026-06-15T00:00:00Z",
  payload: { cpu_millis: 250, memory_bytes: 536870912, player_count: 4 },
});

describe("parseServerFrame", () => {
  test("status frame", async ({ bench }) => {
    await bench("status frame", () => {
      parseServerFrame(STATUS_FRAME);
    }).run();
  });

  test("log frame", async ({ bench }) => {
    await bench("log frame", () => {
      parseServerFrame(LOG_FRAME);
    }).run();
  });

  test("metrics frame", async ({ bench }) => {
    await bench("metrics frame", () => {
      parseServerFrame(METRICS_FRAME);
    }).run();
  });
});
