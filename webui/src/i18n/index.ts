import { en } from "./en.ts";

// Keys are derived from the shipped dictionary, so a missing key fails
// typecheck. No i18n library — WEBUI_SPEC.md Section 7.5 only requires that all
// strings sit behind a dictionary; English is shipped, Japanese is addable as a
// sibling object with the same keys.
export type TranslationKey = keyof typeof en;

export function t(key: TranslationKey): string {
  return en[key];
}
