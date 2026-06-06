import { NavLink, Outlet } from "react-router";
import { type TranslationKey, t } from "../i18n/index.ts";

// Authenticated shell chrome: left nav (community scope + admin group) and a
// top bar (community switcher + user menu). Routing-only for Phase 1 — the
// community switcher and user menu are static placeholders (WEBUI_SPEC.md
// Section 5). A fixed demo community id keeps the nav links resolvable until
// real community data lands.
const DEMO_CID = "demo";

interface NavSpec {
  to: string;
  icon: string;
  labelKey: TranslationKey;
}

const communityNav: NavSpec[] = [
  { to: `/communities/${DEMO_CID}`, icon: "▦", labelKey: "nav.dashboard" },
  {
    to: `/communities/${DEMO_CID}/servers/new`,
    icon: "+",
    labelKey: "nav.createServer",
  },
  {
    to: `/communities/${DEMO_CID}/settings`,
    icon: "⚙",
    labelKey: "nav.settings",
  },
];

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
    >
      <span className="ico" aria-hidden="true">
        {icon}
      </span>
      {t(labelKey)}
    </NavLink>
  );
}

export function AppShell() {
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="cube" aria-hidden="true" />
          {t("shell.brand")}
        </div>
        <nav className="nav-group">
          <div className="nav-label">{t("nav.community")}</div>
          {communityNav.map((item) => (
            <NavItem key={item.to} {...item} />
          ))}
        </nav>
        <nav className="nav-group">
          <div className="nav-label">{t("nav.admin")}</div>
          {adminNav.map((item) => (
            <NavItem key={item.to} {...item} />
          ))}
        </nav>
      </aside>
      <div className="main">
        <header className="topbar">
          {/* Intentionally inert Phase-1 placeholder; real switching lands later. */}
          <div className="community-switcher">
            {t("nav.community")} <span className="chev">▼</span>
          </div>
          <div className="spacer" />
          <NavLink className="user-menu" to="/account">
            <span className="avatar" aria-hidden="true">
              A
            </span>
            {t("shell.account")}
          </NavLink>
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
