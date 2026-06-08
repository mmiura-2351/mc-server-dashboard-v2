import { Link } from "react-router";
import { t } from "../i18n/index.ts";
import { LANDING_PATH } from "../routes.ts";

// Not-found (404) route: rendered for any unmatched URL (#639). The catch-all
// route lives outside the shell (no sidebar/header), so a mistyped URL would
// otherwise strand the user; this presents real not-found copy and a link back
// to the landing route, which resolves to the user's dashboard (or /login when
// signed out). PlaceholderPage is intentionally not used here so users never
// see the developer placeholder text (#584/#593).
export function NotFoundPage() {
  return (
    <div className="empty">
      <h1 className="big">{t("page.notFound")}</h1>
      <p className="sub">{t("notFound.body")}</p>
      <Link className="btn primary" to={LANDING_PATH}>
        {t("notFound.home")}
      </Link>
    </div>
  );
}
