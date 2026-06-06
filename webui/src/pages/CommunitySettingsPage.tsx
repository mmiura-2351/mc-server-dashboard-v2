import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";
import { api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import { type TranslationKey, t } from "../i18n/index.ts";
import { type Can, useCan } from "../permissions/useCan.ts";
import { dashboardPath } from "../routes.ts";
import { CommunityGeneralTab } from "./CommunityGeneralTab.tsx";
import { CommunityGrantsTab } from "./CommunityGrantsTab.tsx";
import { CommunityGroupsTab } from "./CommunityGroupsTab.tsx";
import { CommunityMembersTab } from "./CommunityMembersTab.tsx";
import { PlaceholderPage } from "./PlaceholderPage.tsx";

// Tab order mirrors the mockup (docs/ui/mockup/community-settings.html). Members
// and General ship here; Roles/Grants/Groups/Audit render placeholders until
// their sibling issues (#462–#465) land — each as one import + one case below.
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
  const [tab, setTab] = useState<Tab>("members");
  const query = useQuery({
    queryKey: ["communities", communityId],
    queryFn: () =>
      api.get(
        apiPath("/communities/{community_id}", { community_id: communityId }),
      ),
  });

  if (query.isPending) {
    return <p className="sub">{t("communitySettings.loading")}</p>;
  }
  if (query.isError || query.data === undefined) {
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
            type="button"
            role="tab"
            aria-selected={tab === name}
            className={`tab${tab === name ? " active" : ""}`}
            onClick={() => setTab(name)}
          >
            {t(TAB_LABEL[name])}
          </button>
        ))}
      </div>
      <TabContent
        tab={tab}
        communityId={communityId}
        community={community}
        can={can}
      />
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
    case "general":
      return <CommunityGeneralTab community={community} can={can} />;
    default:
      // Roles / Grants / Groups / Audit arrive with their sibling issues; each
      // replaces this case with one import + one branch (#462–#465).
      return <PlaceholderPage titleKey={TAB_LABEL[tab]} />;
  }
}
