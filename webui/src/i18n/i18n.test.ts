import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { en } from "./en.ts";
import { getLanguage, initLanguage, setLanguage, t } from "./index.ts";
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

  it("setLanguage persists the choice and applies it on the next boot", () => {
    // Stub reload so setLanguage does not blow up jsdom.
    const reload = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({
      ...window.location,
      reload,
    } as Location);

    setLanguage("ja");
    expect(reload).toHaveBeenCalledOnce();
    expect(localStorage.getItem("mcsd.lang")).toBe("ja");

    // A fresh boot reads the persisted override.
    initLanguage();
    expect(getLanguage()).toBe("ja");
  });
});
