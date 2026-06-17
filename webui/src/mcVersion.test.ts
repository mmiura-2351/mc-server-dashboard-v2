import { describe, expect, it } from "vitest";
import { supportsResourcePackOptions } from "./mcVersion.ts";

describe("supportsResourcePackOptions", () => {
  it("returns true for 1.17", () => {
    expect(supportsResourcePackOptions("1.17")).toBe(true);
  });

  it("returns true for 1.17.1", () => {
    expect(supportsResourcePackOptions("1.17.1")).toBe(true);
  });

  it("returns true for 1.21.6", () => {
    expect(supportsResourcePackOptions("1.21.6")).toBe(true);
  });

  it("returns false for 1.16.4", () => {
    expect(supportsResourcePackOptions("1.16.4")).toBe(false);
  });

  it("returns false for 1.16", () => {
    expect(supportsResourcePackOptions("1.16")).toBe(false);
  });

  it("returns false for 1.0", () => {
    expect(supportsResourcePackOptions("1.0")).toBe(false);
  });

  it("returns true for snapshot versions", () => {
    expect(supportsResourcePackOptions("24w03a")).toBe(true);
    expect(supportsResourcePackOptions("21w15a")).toBe(true);
  });

  it("returns true for null", () => {
    expect(supportsResourcePackOptions(null)).toBe(true);
  });

  it("returns true for undefined", () => {
    expect(supportsResourcePackOptions(undefined)).toBe(true);
  });

  it("returns true for empty string", () => {
    expect(supportsResourcePackOptions("")).toBe(true);
  });

  it("returns true for pre-release versions", () => {
    expect(supportsResourcePackOptions("1.17-pre1")).toBe(true);
  });

  it("returns true for major version > 1", () => {
    expect(supportsResourcePackOptions("2.0")).toBe(true);
  });
});
