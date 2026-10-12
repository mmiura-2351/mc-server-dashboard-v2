import { Link } from "react-router";
import { t } from "../i18n/index.ts";
import { LANDING_PATH } from "../routes.ts";

// Keep a home link outside the shell so unmatched URLs do not strand signed-out users.
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
