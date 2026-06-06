import { afterEach, describe, expect, it } from "vitest";
import {
  clearAccessToken,
  getAccessToken,
  setAccessToken,
} from "./tokenStore.ts";

describe("tokenStore", () => {
  afterEach(() => {
    clearAccessToken();
  });

  it("starts empty", () => {
    expect(getAccessToken()).toBeNull();
  });

  it("stores and returns the access token", () => {
    setAccessToken("abc");
    expect(getAccessToken()).toBe("abc");
  });

  it("clears the access token", () => {
    setAccessToken("abc");
    clearAccessToken();
    expect(getAccessToken()).toBeNull();
  });

  it("keeps the token in memory only, never in web storage", () => {
    setAccessToken("secret");
    expect(JSON.stringify(localStorage)).not.toContain("secret");
    expect(JSON.stringify(sessionStorage)).not.toContain("secret");
  });
});
