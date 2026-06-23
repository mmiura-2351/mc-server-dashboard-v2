import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { en } from "./en.ts";
import {
  getLanguage,
  initLanguage,
  setLanguage,
  subscribeLanguage,
  t,
} from "./index.ts";
import { ja } from "./ja.ts";

describe("dictionaries", () => {
  it("ja has exactly the same key set as en", () => {
    expect(Object.keys(ja).sort()).toEqual(Object.keys(en).sort());
  });

  it("ja has no empty strings", () => {
    for (const [key, value] of Object.entries(ja)) {
      expect(value, key).not.toBe("");
    }
  });
});

describe("t() interpolation", () => {
  beforeEach(() => {
    localStorage.clear();
    initLanguage(); // en default in jsdom
  });

  afterEach(() => {
    localStorage.clear();
    initLanguage();
  });

  it("returns the raw template when no params are given", () => {
    expect(t("admin.versions.refreshedOne")).toBe("Refreshed catalog: {type}");
  });

  it("substitutes a single named token", () => {
    expect(t("admin.versions.refreshedOne", { type: "paper" })).toBe(
      "Refreshed catalog: paper",
    );
  });

  it("substitutes multiple tokens and stringifies numbers", () => {
    expect(t("admin.versions.gcDone", { bytes: "412.0 MiB", count: 3 })).toBe(
      "Freed 412.0 MiB by deleting 3 unused JARs.",
    );
  });

  it("leaves unknown tokens verbatim", () => {
    expect(t("admin.versions.refreshedOne", { other: "x" })).toBe(
      "Refreshed catalog: {type}",
    );
  });

  it("interpolates against the active language", () => {
    setLanguage("ja");
    expect(t("admin.versions.refreshedOne", { type: "paper" })).toBe(
      ja["admin.versions.refreshedOne"].replace("{type}", "paper"),
    );
  });
});

describe("language detection and override", () => {
  beforeEach(() => {
    localStorage.clear();
    initLanguage(); // reset to default (en) — no override, jsdom navigator is en
  });

  afterEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
    initLanguage();
  });

  it("defaults to English without an override or ja browser", () => {
    expect(getLanguage()).toBe("en");
    expect(t("app.title")).toBe("mc-server-dashboard");
    expect(t("shell.account")).toBe("Account");
  });

  it("detects Japanese from navigator.language on boot", () => {
    vi.spyOn(navigator, "language", "get").mockReturnValue("ja-JP");
    initLanguage();
    expect(getLanguage()).toBe("ja");
    expect(t("shell.account")).toBe(ja["shell.account"]);
  });

  it("a stored override wins over the browser language", () => {
    vi.spyOn(navigator, "language", "get").mockReturnValue("ja-JP");
    localStorage.setItem("mcsd.lang", "en");
    initLanguage();
    expect(getLanguage()).toBe("en");
  });

  it("setLanguage persists the choice, applies it live, and notifies", () => {
    const notified = vi.fn();
    const unsubscribe = subscribeLanguage(notified);

    setLanguage("ja");

    // Applied in place (no reload) and persisted, and subscribers are notified
    // so the app re-renders against the new dictionary (issues #515, #512).
    expect(getLanguage()).toBe("ja");
    expect(t("shell.account")).toBe(ja["shell.account"]);
    expect(localStorage.getItem("mcsd.lang")).toBe("ja");
    expect(notified).toHaveBeenCalledOnce();

    // A fresh boot reads the persisted override.
    initLanguage();
    expect(getLanguage()).toBe("ja");

    unsubscribe();
  });

  it("setLanguage to the current language is a no-op (no notify)", () => {
    const notified = vi.fn();
    const unsubscribe = subscribeLanguage(notified);

    setLanguage("en"); // already en
    expect(notified).not.toHaveBeenCalled();

    unsubscribe();
  });
});
