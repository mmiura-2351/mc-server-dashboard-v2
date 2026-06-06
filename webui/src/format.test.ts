import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { heartbeatAge, humanizeBytes, statusPill } from "./format.ts";

describe("humanizeBytes", () => {
  it("renders sub-KiB values as plain bytes", () => {
    expect(humanizeBytes(0)).toBe("0 B");
    expect(humanizeBytes(1023)).toBe("1023 B");
  });

  it("scales into binary units with one decimal", () => {
    expect(humanizeBytes(1024)).toBe("1.0 KiB");
    expect(humanizeBytes(1610612736)).toBe("1.5 GiB");
  });

  it("clamps at the largest unit (TiB)", () => {
    expect(humanizeBytes(1024 ** 5)).toBe("1024.0 TiB");
  });
});

describe("statusPill", () => {
  it("maps online to running and draining to starting", () => {
    expect(statusPill("online")).toBe("running");
    expect(statusPill("draining")).toBe("starting");
  });

  it("maps anything else to crashed", () => {
    expect(statusPill("offline")).toBe("crashed");
    expect(statusPill("unknown")).toBe("crashed");
  });
});

describe("heartbeatAge", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders seconds below a minute", () => {
    expect(heartbeatAge("2025-12-31T23:59:58Z")).toBe("2s ago");
  });

  it("renders minutes below an hour", () => {
    expect(heartbeatAge("2025-12-31T23:56:00Z")).toBe("4m ago");
  });

  it("renders hours at and above an hour", () => {
    expect(heartbeatAge("2025-12-31T21:00:00Z")).toBe("3h ago");
  });

  it("clamps future timestamps to 0s", () => {
    expect(heartbeatAge("2026-01-01T00:00:05Z")).toBe("0s ago");
  });
});
