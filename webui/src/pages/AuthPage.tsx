import { Link } from "react-router";
import { type TranslationKey, t } from "../i18n/index.ts";

// Login / register placeholders. These render outside the shell chrome
// (WEBUI_SPEC.md Section 5). Phase 1 ships layout only — no real forms or auth.
interface AuthPageProps {
  titleKey: TranslationKey;
  altKey: TranslationKey;
  altTo: string;
}

export function AuthPage({ titleKey, altKey, altTo }: AuthPageProps) {
  return (
    <div className="auth-wrap">
      <div className="card auth-card">
        <div className="brand">
          <span className="cube" aria-hidden="true" />
          {t("shell.brand")}
        </div>
        <h1>{t(titleKey)}</h1>
        <p className="sub">{t("page.placeholder")}</p>
        <div className="alt">
          <Link to={altTo}>{t(altKey)}</Link>
        </div>
      </div>
    </div>
  );
}
