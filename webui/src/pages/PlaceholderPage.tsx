import { type TranslationKey, t } from "../i18n/index.ts";

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
