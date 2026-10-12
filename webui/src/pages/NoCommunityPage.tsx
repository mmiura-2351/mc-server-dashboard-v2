import { Link } from "react-router";
import { useCurrentUser } from "../auth/useCurrentUser.ts";
import { t } from "../i18n/index.ts";

// Community provisioning requires an admin; other users can only ask to be added.
export function NoCommunityPage() {
  const isAdmin = useCurrentUser().data?.is_platform_admin === true;
  return (
    <div className="empty">
      <h1 className="big">{t("noCommunity.title")}</h1>
      <p className="sub">{t("noCommunity.body")}</p>
      {isAdmin ? (
        <>
          <p className="sub">{t("noCommunity.adminHint")}</p>
          <Link className="btn primary" to="/admin/communities">
            {t("noCommunity.adminCta")}
          </Link>
        </>
      ) : (
        <p className="sub">{t("noCommunity.memberHint")}</p>
      )}
    </div>
  );
}
