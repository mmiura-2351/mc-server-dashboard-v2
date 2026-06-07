import { Link } from "react-router";
import { useCurrentUser } from "../auth/useCurrentUser.ts";
import { t } from "../i18n/index.ts";

// Empty state for the landing route when the signed-in account belongs to zero
// communities (#584). There is no self-serve create/join flow for a normal
// user — provisioning a community requires platform admin — so this is an
// informational view: explain the state and point to the next action. A
// platform admin gets a CTA to create the first community; everyone else is
// told to ask an admin to be added.
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
