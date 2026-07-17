import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import { type TranslationKey, t } from "../i18n/index.ts";
import { type Can, useCan } from "../permissions/useCan.ts";
import { dashboardPath } from "../routes.ts";
import { CommunityAuditTab } from "./CommunityAuditTab.tsx";
import { CommunityGeneralTab } from "./CommunityGeneralTab.tsx";
import { CommunityGrantsTab } from "./CommunityGrantsTab.tsx";
import { CommunityGroupsTab } from "./CommunityGroupsTab.tsx";
import { CommunityMembersTab } from "./CommunityMembersTab.tsx";
import { CommunityRolesTab } from "./CommunityRolesTab.tsx";
import { handleTabKeyDown, panelId, tabId, useTabHash } from "./urlState.ts";

// Tab order mirrors the mockup (docs/ui/mockup/community-settings.html).
const TABS = [
  "members",
  "roles",
  "grants",
  "groups",
  "audit",
  "general",
] as const;
type Tab = (typeof TABS)[number];

const TAB_LABEL: Record<Tab, TranslationKey> = {
  members: "communitySettings.tab.members",
  roles: "communitySettings.tab.roles",
  grants: "communitySettings.tab.grants",
  groups: "communitySettings.tab.groups",
  audit: "communitySettings.tab.audit",
  general: "communitySettings.tab.general",
};

export function CommunitySettingsPage() {
  const { cid } = useParams();
  if (cid === undefined) {
    return null;
  }
  return <Loaded communityId={cid} />;
}

function Loaded({ communityId }: { communityId: string }) {
  const can = useCan();
  // Active tab lives in the URL hash (#514) so Back walks the tab history;
  // #members is the default and keeps a clean URL.
  const [tab, setTab] = useTabHash(TABS);
  const query = useQuery({
    queryKey: ["communities", communityId],
    queryFn: ({ signal }) =>
      api.get(
        apiPath("/api/communities/{community_id}", {
          community_id: communityId,
        }),
        { signal },
      ),
  });

  if (query.isPending) {
    return <p className="sub">{t("communitySettings.loading")}</p>;
  }
  // Full-page error only when there is nothing to show (the initial load
  // failed). A failed background refetch retains `data`, so the cached page
  // keeps rendering through transient API blips (#1797).
  if (query.data === undefined) {
    return <p className="field-error">{t("communitySettings.loadError")}</p>;
  }

  const community = query.data;
  return (
    <>
      <div className="page-head">
        <div>
          <div className="breadcrumbs">
            <Link to={dashboardPath(communityId)}>
              {t("communitySettings.breadcrumb")}
            </Link>{" "}
            / {community.name}
          </div>
          <h1 className="detail-title">{community.name}</h1>
        </div>
      </div>
      <div className="tabs" role="tablist">
        {TABS.map((name) => (
          <button
            key={name}
            id={tabId("cs", name)}
            type="button"
            role="tab"
            aria-selected={tab === name}
            aria-controls={panelId("cs", name)}
            tabIndex={tab === name ? 0 : -1}
            className={`tab${tab === name ? " active" : ""}`}
            onClick={() => setTab(name)}
            onKeyDown={(e) => handleTabKeyDown(e, TABS, tab, setTab, "cs")}
          >
            {t(TAB_LABEL[name])}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id={panelId("cs", tab)}
        aria-labelledby={tabId("cs", tab)}
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a focusable tabpanel is the APG tabs pattern itself; the rule has no role exception (eslint-jsx-a11y exempts role="tabpanel" by default).
        tabIndex={0}
      >
        <TabContent
          tab={tab}
          communityId={communityId}
          community={community}
          can={can}
        />
      </div>
    </>
  );
}

function TabContent({
  tab,
  communityId,
  community,
  can,
}: {
  tab: Tab;
  communityId: string;
  community: { id: string; name: string };
  can: Can;
}) {
  switch (tab) {
    case "members":
      return can("member:read") ? (
        <CommunityMembersTab communityId={communityId} can={can} />
      ) : (
        <p className="field-error">{t("permissions.denied")}</p>
      );
    case "roles":
      return can("role:read") ? (
        <CommunityRolesTab communityId={communityId} can={can} />
      ) : (
        <p className="field-error">{t("permissions.denied")}</p>
      );
    case "grants":
      return can("grant:read") ? (
        <CommunityGrantsTab communityId={communityId} can={can} />
      ) : (
        <p className="field-error">{t("permissions.denied")}</p>
      );
    case "groups":
      return can("group:read") ? (
        <CommunityGroupsTab communityId={communityId} can={can} />
      ) : (
        <p className="field-error">{t("permissions.denied")}</p>
      );
    case "audit":
      return <CommunityAuditTab communityId={communityId} can={can} />;
    case "general":
      return <CommunityGeneralTab community={community} can={can} />;
  }
}
