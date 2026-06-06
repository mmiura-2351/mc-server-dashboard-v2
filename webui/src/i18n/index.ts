import { en } from "./en.ts";
import { ja } from "./ja.ts";

// Keys are derived from the shipped English dictionary, so a missing key fails
// typecheck. No i18n library — WEBUI_SPEC.md Section 7.5 only requires that all
// strings sit behind a dictionary; English is shipped, Japanese is a sibling
// object with the same keys.
export type TranslationKey = keyof typeof en;

export type Language = "en" | "ja";

const dictionaries: Record<Language, Record<TranslationKey, string>> = {
  en,
  ja,
};

const STORAGE_KEY = "mcsd.lang";

// `t()` is called at module/render time across the whole app, so the active
// language is kept in a module-level variable rather than threaded through
// React context. A language switch persists the choice and reloads the page
// (see setLanguage) so every `t()` call re-evaluates against the new
// dictionary — the simplest mechanism that genuinely re-renders the app
// without touching ~50 call sites. Tradeoff: switching costs a full reload.
let currentLanguage: Language = "en";

function detectLanguage(): Language {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "en" || stored === "ja") {
    return stored;
  }
  return navigator.language.startsWith("ja") ? "ja" : "en";
}

/** Resolve the active language on boot (localStorage override, else browser). */
export function initLanguage(): void {
  currentLanguage = detectLanguage();
}

export function getLanguage(): Language {
  return currentLanguage;
}

/** Persist the chosen language and reload so every `t()` call re-evaluates. */
export function setLanguage(lang: Language): void {
  localStorage.setItem(STORAGE_KEY, lang);
  currentLanguage = lang;
  location.reload();
}

export function t(key: TranslationKey): string {
  return dictionaries[currentLanguage][key];
}
