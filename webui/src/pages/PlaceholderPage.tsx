import { type TranslationKey, t } from "../i18n/index.ts";

// Minimal placeholder for a routed screen. Phase 1 ships the routing skeleton
// only; real screen content arrives in later phases (WEBUI_SPEC.md Section 5).
export function PlaceholderPage({ titleKey }: { titleKey: TranslationKey }) {
  return (
    <>
      <div className="page-head">
        <h1>{t(titleKey)}</h1>
      </div>
      <p className="sub">{t("page.placeholder")}</p>
    </>
  );
}
