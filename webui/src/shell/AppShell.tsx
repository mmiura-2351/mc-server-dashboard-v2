import { Suspense, useEffect } from "react";
import { Link, NavLink, Outlet, useNavigate, useParams } from "react-router";
import { useCurrentUser } from "../auth/useCurrentUser.ts";
import {
  getLanguage,
  type Language,
  setLanguage,
  type TranslationKey,
  t,
} from "../i18n/index.ts";
import { useActiveCommunity } from "../permissions/ActiveCommunityProvider.tsx";
import { dashboardPath, LANDING_PATH } from "../routes.ts";

// Authenticated shell chrome: left nav (community scope + admin group) and a
// top bar (community switcher + user menu) (WEBUI_SPEC.md Section 5). The
// community scope resolves from the active community (#412); the switcher lists
// the caller's communities and switching navigates to that community's
// dashboard.

interface NavSpec {
  to: string;
  icon: string;
  labelKey: TranslationKey;
}

// Community-scoped nav for the active community. Computed per-render from the
// active id so switching re-targets every link.
function communityNav(cid: string): NavSpec[] {
  const base = dashboardPath(cid);
  return [
    { to: base, icon: "▦", labelKey: "nav.dashboard" },
    { to: `${base}/servers/new`, icon: "+", labelKey: "nav.createServer" },
    { to: `${base}/settings`, icon: "⚙", labelKey: "nav.settings" },
  ];
}

const adminNav: NavSpec[] = [
  { to: "/admin", icon: "◎", labelKey: "nav.adminOverview" },
  { to: "/admin/users", icon: "👤", labelKey: "nav.adminUsers" },
  { to: "/admin/communities", icon: "▣", labelKey: "nav.adminCommunities" },
  { to: "/admin/workers", icon: "🖧", labelKey: "nav.adminWorkers" },
  { to: "/admin/versions", icon: "⬇", labelKey: "nav.adminVersions" },
  { to: "/admin/audit", icon: "≡", labelKey: "nav.adminAudit" },
];

function NavItem({ to, icon, labelKey }: NavSpec) {
  return (
    <NavLink
      to={to}
      end
      className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
      aria-label={t(labelKey)}
    >
      <span className="ico" aria-hidden="true">
        {icon}
      </span>
      <span className="label">{t(labelKey)}</span>
    </NavLink>
  );
}

// Top-bar switcher: lists the caller's communities and switches the active one.
// While the list is loading it renders a non-interactive placeholder so the bar
// does not flicker; with no communities it shows a disabled hint.
function CommunitySwitcher() {
  const { communityId, setCommunityId, communities } = useActiveCommunity();
  const navigate = useNavigate();

  if (communities === undefined) {
    return (
      <div className="community-switcher" aria-busy="true">
        {t("auth.loading")}
      </div>
    );
  }

  if (communities.length === 0) {
    return (
      <div className="community-switcher no-community">
        {t("shell.noCommunity")}
      </div>
    );
  }

  return (
    <select
      className="community-switcher"
      aria-label={t("shell.switchCommunity")}
      value={communityId ?? ""}
      onChange={(e) => {
        const id = e.target.value;
        setCommunityId(id);
        navigate(dashboardPath(id));
      }}
    >
      {communities.map((c) => (
        <option key={c.id} value={c.id}>
          {c.name}
        </option>
      ))}
    </select>
  );
}

// Keeps the active community in sync with the URL: deep-linking to a community
// the caller belongs to selects it (URL wins on load). Switching from the top
// bar drives the URL the other way (CommunitySwitcher), so this only reacts to
// cids that actually exist in the caller's list.
function useUrlCommunitySync() {
  const { cid } = useParams();
  const { communityId, setCommunityId, communities } = useActiveCommunity();

  useEffect(() => {
    if (cid === undefined || cid === communityId || communities === undefined) {
      return;
    }
    if (communities.some((c) => c.id === cid)) {
      setCommunityId(cid);
    }
  }, [cid, communityId, communities, setCommunityId]);
}

// Top-bar language selector. Switching persists the choice and reloads so the
// module-level `t()` re-evaluates against the new dictionary (see i18n/index).
function LanguageSwitcher() {
  return (
    <select
      className="community-switcher lang-switcher"
      aria-label={t("shell.language")}
      value={getLanguage()}
      onChange={(e) => setLanguage(e.target.value as Language)}
    >
      <option value="en">{t("shell.language.en")}</option>
      <option value="ja">{t("shell.language.ja")}</option>
    </select>
  );
}

export function AppShell() {
  const { communityId } = useActiveCommunity();
  // The admin nav group renders only for platform admins (#474). Same shared
  // current-user query the admin-route guard reads, so there is one fetch.
  const isAdmin = useCurrentUser().data?.is_platform_admin === true;
  useUrlCommunitySync();

  return (
    <div className="shell">
      <aside className="sidebar">
        <Link className="brand" to={LANDING_PATH} aria-label={t("shell.brand")}>
          <span className="cube" aria-hidden="true" />
          <span className="label">{t("shell.brand")}</span>
        </Link>
        <nav className="nav-group">
          <div className="nav-label">{t("nav.community")}</div>
          {communityId === null ? (
            <div className="nav-hint">{t("shell.noCommunities")}</div>
          ) : (
            communityNav(communityId).map((item) => (
              <NavItem key={item.to} {...item} />
            ))
          )}
        </nav>
        <nav className="nav-group">
          <NavItem
            to="/resource-packs"
            icon="📦"
            labelKey="nav.resourcePacks"
          />
        </nav>
        {isAdmin && (
          <nav className="nav-group">
            <div className="nav-label">{t("nav.admin")}</div>
            {adminNav.map((item) => (
              <NavItem key={item.to} {...item} />
            ))}
          </nav>
        )}
      </aside>
      <div className="main">
        <header className="topbar">
          <CommunitySwitcher />
          <div className="spacer" />
          <LanguageSwitcher />
          <NavLink
            className="user-menu"
            to="/account"
            aria-label={t("shell.account")}
          >
            <span className="avatar" aria-hidden="true">
              A
            </span>
            <span className="label">{t("shell.account")}</span>
          </NavLink>
        </header>
        <main className="content">
          {/* The lazy-route Suspense boundary lives here, around <Outlet>, so a
              not-yet-cached page chunk (#553) suspends only the content area —
              the sidebar/top bar stay mounted instead of the whole shell
              flashing through the app-level fallback (#602). The fallback
              mirrors App's SessionLoading. */}
          <Suspense
            fallback={
              <div className="auth-wrap" role="status">
                {t("auth.loading")}
              </div>
            }
          >
            <Outlet />
          </Suspense>
        </main>
      </div>
    </div>
  );
}
