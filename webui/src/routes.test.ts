import { describe, expect, it } from "vitest";
import { LANDING_PATH, postLoginPath } from "./routes.ts";

describe("postLoginPath", () => {
  it("restores a valid path with its query string", () => {
    expect(
      postLoginPath({ pathname: "/communities/c1", search: "?tab=logs" }),
    ).toBe("/communities/c1?tab=logs");
  });

  it("rejects protocol-relative paths (//evil.com)", () => {
    expect(postLoginPath({ pathname: "//evil.com/x", search: "" })).toBe(
      LANDING_PATH,
    );
  });

  it("rejects backslash protocol-relative paths (/\\evil.com)", () => {
    expect(postLoginPath({ pathname: "/\\evil.com", search: "" })).toBe(
      LANDING_PATH,
    );
  });

  it("falls back to LANDING_PATH for missing state", () => {
    expect(postLoginPath(null)).toBe(LANDING_PATH);
    expect(postLoginPath(undefined)).toBe(LANDING_PATH);
  });

  it("falls back to LANDING_PATH for garbage state", () => {
    expect(postLoginPath("not-an-object")).toBe(LANDING_PATH);
    expect(postLoginPath({ pathname: 42 })).toBe(LANDING_PATH);
    expect(postLoginPath({ pathname: "no-leading-slash" })).toBe(LANDING_PATH);
  });
});
