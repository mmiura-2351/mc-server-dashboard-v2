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
// React context. A language switch persists the choice, updates this variable,
// and notifies subscribers so the app re-renders against the new dictionary —
// the React root remounts its subtree (see subscribeLanguage / main.tsx) so
// every `t()` call re-evaluates without touching ~50 call sites.
//
// This used to call `location.reload()` instead, but a reload tears down any
// in-flight session-refresh rotation: its Set-Cookie can be dropped while the
// API has already committed the rotation, leaving a revoked refresh token in
// the cookie jar that the next bootstrap replays and the API revokes the whole
// family for (signed out). Re-rendering in place keeps the session intact
// (issues #515, #512).
let currentLanguage: Language = "en";

// Subscribers notified when the active language changes (the React root, which
// remounts its subtree so `t()` re-evaluates). A plain Set keeps this free of
// React; `useSyncExternalStore` in main.tsx adapts it.
const languageSubscribers = new Set<() => void>();

/** Subscribe to language changes; returns an unsubscribe function. */
export function subscribeLanguage(callback: () => void): () => void {
  languageSubscribers.add(callback);
  return () => {
    languageSubscribers.delete(callback);
  };
}

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

/**
 * Persist the chosen language and notify subscribers so the app re-renders
 * against the new dictionary. A no-op when the language is unchanged.
 */
export function setLanguage(lang: Language): void {
  if (lang === currentLanguage) {
    return;
  }
  localStorage.setItem(STORAGE_KEY, lang);
  currentLanguage = lang;
  for (const callback of languageSubscribers) {
    callback();
  }
}

export function t(key: TranslationKey): string {
  return dictionaries[currentLanguage][key];
}
