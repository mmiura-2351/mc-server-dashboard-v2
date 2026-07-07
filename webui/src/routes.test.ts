// @vitest-environment node
// DOM-free logic test; runs under Node to skip per-file jsdom setup (issue #1734).
import { describe, expect, it } from "vitest";
import {
  expiredLoginPath,
  LANDING_PATH,
  postLoginPath,
  safeNextPath,
} from "./routes.ts";

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

describe("safeNextPath", () => {
  it("accepts an in-app relative path with query and hash", () => {
    expect(safeNextPath("/communities/c1/servers/s1?tab=logs#h")).toBe(
      "/communities/c1/servers/s1?tab=logs#h",
    );
    expect(safeNextPath("/")).toBe("/");
  });

  it("rejects protocol-relative paths (//evil.com)", () => {
    expect(safeNextPath("//evil.com")).toBeNull();
    expect(safeNextPath("//evil.com/path")).toBeNull();
  });

  it("rejects backslash protocol-relative paths (/\\evil.com)", () => {
    expect(safeNextPath("/\\evil.com")).toBeNull();
  });

  it("rejects absolute URLs", () => {
    expect(safeNextPath("https://evil.com")).toBeNull();
    expect(safeNextPath("http://evil.com/path")).toBeNull();
  });

  it("rejects scheme URIs", () => {
    expect(safeNextPath("javascript:alert(1)")).toBeNull();
    expect(safeNextPath("data:text/html,x")).toBeNull();
  });

  it("rejects values without a leading slash", () => {
    expect(safeNextPath("evil.com")).toBeNull();
    expect(safeNextPath("")).toBeNull();
  });

  it("rejects non-string input", () => {
    expect(safeNextPath(null)).toBeNull();
    expect(safeNextPath(undefined)).toBeNull();
    expect(safeNextPath(42)).toBeNull();
  });

  it("rejects auth routes to avoid a login loop", () => {
    expect(safeNextPath("/login")).toBeNull();
    expect(safeNextPath("/login?next=/x")).toBeNull();
    expect(safeNextPath("/register")).toBeNull();
  });
});

describe("expiredLoginPath", () => {
  it("carries reason=expired and the validated location as next", () => {
    const result = expiredLoginPath({
      pathname: "/communities/c1/servers/s1",
      search: "?tab=logs",
      hash: "#h",
    });
    const params = new URLSearchParams(result.replace(/^\/login\?/, ""));
    expect(params.get("reason")).toBe("expired");
    expect(params.get("next")).toBe("/communities/c1/servers/s1?tab=logs#h");
  });

  it("omits next when the current location is an auth route", () => {
    const result = expiredLoginPath({
      pathname: "/login",
      search: "",
      hash: "",
    });
    const params = new URLSearchParams(result.replace(/^\/login\?/, ""));
    expect(params.get("reason")).toBe("expired");
    expect(params.get("next")).toBeNull();
  });
});
