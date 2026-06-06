import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearAccessToken,
  getAccessToken,
  onAccessTokenRotation,
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

describe("tokenStore rotation listeners", () => {
  afterEach(() => {
    clearAccessToken();
  });

  it("notifies on a rotation to a different token", () => {
    setAccessToken("first");
    const listener = vi.fn();
    const off = onAccessTokenRotation(listener);

    setAccessToken("second");
    expect(listener).toHaveBeenCalledTimes(1);
    off();
  });

  it("does not notify on the initial set (no prior token)", () => {
    const listener = vi.fn();
    const off = onAccessTokenRotation(listener);

    setAccessToken("first");
    expect(listener).not.toHaveBeenCalled();
    off();
  });

  it("does not notify on a no-op re-set of the same token", () => {
    setAccessToken("same");
    const listener = vi.fn();
    const off = onAccessTokenRotation(listener);

    setAccessToken("same");
    expect(listener).not.toHaveBeenCalled();
    off();
  });

  it("stops notifying after unsubscribe", () => {
    setAccessToken("first");
    const listener = vi.fn();
    onAccessTokenRotation(listener)();

    setAccessToken("second");
    expect(listener).not.toHaveBeenCalled();
  });
});
