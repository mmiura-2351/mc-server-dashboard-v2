import { en } from "./en.ts";
import { ja } from "./ja.ts";

// Derive translation keys from English so missing keys fail type checking.
export type TranslationKey = keyof typeof en;

export type Language = "en" | "ja";

const dictionaries: Record<Language, Record<TranslationKey, string>> = {
  en,
  ja,
};

const STORAGE_KEY = "mcsd.lang";

// Switch languages in place to avoid interrupting an in-flight refresh-cookie rotation.
// Remount the app below the session and query providers so translated calls rerun without losing either state.
let currentLanguage: Language = "en";

// Keep subscriptions framework-free; main.tsx adapts them with useSyncExternalStore.
const languageSubscribers = new Set<() => void>();

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

export function initLanguage(): void {
  currentLanguage = detectLanguage();
}

export function getLanguage(): Language {
  return currentLanguage;
}

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

/** Interpolate whole sentences to preserve each language's word order; leave unmatched tokens unchanged. */
export function t(
  key: TranslationKey,
  params?: Record<string, string | number>,
): string {
  const template = dictionaries[currentLanguage][key];
  if (params === undefined) {
    return template;
  }
  return template.replace(/\{(\w+)\}/g, (match, token: string) =>
    token in params ? String(params[token]) : match,
  );
}
