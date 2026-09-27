// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { describe, expect, it } from "vitest";
import {
  readsServerPropertiesAsUtf8,
  supportsResourcePackOptions,
} from "./mcVersion.ts";

describe("supportsResourcePackOptions", () => {
  it.each([
    ["below the 1.17 boundary", "1.16.4", false],
    ["at the 1.17 boundary", "1.17", true],
    ["after the 1.17 boundary", "1.21.6", true],
    ["a snapshot", "24w03a", true],
    ["an unknown version", null, true],
  ])("returns %s", (_partition, version, expected) => {
    expect(supportsResourcePackOptions(version)).toBe(expected);
  });
});

describe("readsServerPropertiesAsUtf8 (#2851)", () => {
  it("returns false for a release below 1.20", () => {
    expect(readsServerPropertiesAsUtf8("1.19.4")).toBe(false);
    expect(readsServerPropertiesAsUtf8("1.19")).toBe(false);
    expect(readsServerPropertiesAsUtf8("1.12.2")).toBe(false);
  });

  it("returns true for 1.20 and later releases", () => {
    expect(readsServerPropertiesAsUtf8("1.20")).toBe(true);
    expect(readsServerPropertiesAsUtf8("1.20.1")).toBe(true);
    expect(readsServerPropertiesAsUtf8("1.21.6")).toBe(true);
    expect(readsServerPropertiesAsUtf8("2.0")).toBe(true);
  });

  it("returns true for snapshot and pre-release versions", () => {
    expect(readsServerPropertiesAsUtf8("23w12a")).toBe(true);
    expect(readsServerPropertiesAsUtf8("1.20-pre1")).toBe(true);
  });

  it("returns true for null, undefined, and empty string", () => {
    expect(readsServerPropertiesAsUtf8(null)).toBe(true);
    expect(readsServerPropertiesAsUtf8(undefined)).toBe(true);
    expect(readsServerPropertiesAsUtf8("")).toBe(true);
  });
});
