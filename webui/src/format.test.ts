import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  formatDateTime,
  formatRange,
  heartbeatAge,
  humanizeBytes,
  shortId,
  statusPill,
} from "./format.ts";
import { setLanguage, t } from "./i18n/index.ts";

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

describe("formatDateTime", () => {
  it("renders an ISO timestamp in the viewer's locale, dropping microseconds", () => {
    const iso = "2026-06-05T13:46:35.411582Z";
    // Locale/timezone-dependent, so assert against the same toLocaleString path
    // rather than a hard-coded string — what matters is the raw ISO no longer
    // leaks through (no "T", no microseconds, no offset).
    const out = formatDateTime(iso);
    expect(out).toBe(new Date(iso).toLocaleString());
    expect(out).not.toContain("T");
    expect(out).not.toContain("411582");
  });
});

describe("shortId", () => {
  it("keeps only the leading UUID segment", () => {
    expect(shortId("ad1051a7-1234-5678-9abc-def012345678")).toBe("ad1051a7");
  });

  it("returns a value without dashes unchanged", () => {
    expect(shortId("miura")).toBe("miura");
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

describe("formatRange", () => {
  it("converts lower-bounded inclusive to 'version+'", () => {
    expect(formatRange("[1.9.10,)")).toBe("1.9.10+");
    expect(formatRange("[1.0,)")).toBe("1.0+");
  });

  it("converts upper-bounded exclusive to '< version'", () => {
    expect(formatRange("(,2.0)")).toBe("< 2.0");
  });

  it("converts upper-bounded inclusive to '<= version'", () => {
    expect(formatRange("(,2.0]")).toBe("<= 2.0");
  });

  it("converts closed ranges to 'lo - hi'", () => {
    expect(formatRange("[1.5,1.8]")).toBe("1.5 – 1.8");
    expect(formatRange("(1.0,2.0)")).toBe("1.0 – 2.0");
  });

  it("returns empty for wildcard or empty input", () => {
    expect(formatRange("*")).toBe("");
    expect(formatRange("")).toBe("");
  });

  it("passes through non-Maven ranges as-is", () => {
    expect(formatRange(">=0.100.0")).toBe(">=0.100.0");
    expect(formatRange("~1.21-")).toBe("~1.21-");
    expect(formatRange("1.0.0")).toBe("1.0.0");
  });
});

describe("heartbeatAge", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
    setLanguage("en");
  });

  it("renders seconds below a minute", () => {
    expect(heartbeatAge("2025-12-31T23:59:58Z", t)).toBe("2s ago");
  });

  it("renders minutes below an hour", () => {
    expect(heartbeatAge("2025-12-31T23:56:00Z", t)).toBe("4m ago");
  });

  it("renders hours at and above an hour", () => {
    expect(heartbeatAge("2025-12-31T21:00:00Z", t)).toBe("3h ago");
  });

  it("clamps future timestamps to 0s", () => {
    expect(heartbeatAge("2026-01-01T00:00:05Z", t)).toBe("0s ago");
  });

  it("renders in Japanese when the locale is ja", () => {
    setLanguage("ja");
    expect(heartbeatAge("2025-12-31T23:59:58Z", t)).toBe("2秒前");
    expect(heartbeatAge("2025-12-31T23:56:00Z", t)).toBe("4分前");
    expect(heartbeatAge("2025-12-31T21:00:00Z", t)).toBe("3時間前");
  });
});
