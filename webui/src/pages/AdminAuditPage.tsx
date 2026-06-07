import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client.ts";
import { t } from "../i18n/index.ts";
import {
  AuditFilterFields,
  type AuditFilters,
  AuditPaging,
  AuditTable,
  applyAuditParams,
  EMPTY_FILTERS,
  isUuid,
  PAGE_SIZE,
} from "./auditShared.tsx";

// The global view adds a `community` filter (empty = all communities) on top of
// the shared operation/actor/since/until filters.
interface AdminAuditFilters extends AuditFilters {
  community: string;
}

const EMPTY: AdminAuditFilters = { ...EMPTY_FILTERS, community: "" };

// Build the global audit-list URL: the shared filters plus the optional
// `community` param, mapped to the exact query params the endpoint declares
// (community/operation/actor/since/until/limit/offset — schema.ts
// list_audit_log). Blank filters are omitted. The result is cast back to the
// path literal so the typed response stays AuditLogResponse.
function auditUrl(filters: AdminAuditFilters, offset: number): "/audit" {
  const params = new URLSearchParams();
  if (filters.community !== "") {
    params.set("community", filters.community);
  }
  applyAuditParams(params, filters);
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(offset));
  return `/audit?${params.toString()}` as "/audit";
}

// Platform-admin global Audit page (WEBUI_SPEC.md 6.12): the global audit log
// with a community filter added. Same filterable/paged shape as the
// community-scoped tab, plus a community picker and a Community column. The
// route is gated by RequireAdmin (App.tsx); the `/audit` endpoint is guarded by
// platform-admin server-side (audit.py).
export function AdminAuditPage() {
  // The community picker lists ALL communities (admins see every community), not
  // just the admin's own — sourced from the platform-axis GET /admin/communities
  // (#489), the same endpoint the admin Communities page uses. One full page is
  // enough to populate the dropdown for current deployments.
  const communitiesQuery = useQuery({
    queryKey: ["admin", "communities", 0],
    queryFn: () =>
      api.get("/admin/communities?limit=100&offset=0" as "/admin/communities"),
  });
  const communities = communitiesQuery.data?.communities;
  // Applied filters (committed on Apply) and the in-progress input draft.
  const [filters, setFilters] = useState<AdminAuditFilters>(EMPTY);
  const [draft, setDraft] = useState<AdminAuditFilters>(EMPTY);
  const [offset, setOffset] = useState(0);
  const [actorError, setActorError] = useState(false);

  const query = useQuery({
    queryKey: ["admin", "audit", filters, offset],
    queryFn: () => api.get(auditUrl(filters, offset)),
    placeholderData: keepPreviousData,
  });

  const apply = () => {
    // The backend's `actor` param is a UUID; reject free text inline rather
    // than letting it 422 into the generic load error.
    if (draft.actor.trim() !== "" && !isUuid(draft.actor.trim())) {
      setActorError(true);
      return;
    }
    setActorError(false);
    setFilters(draft);
    setOffset(0);
  };

  const records = query.data?.records ?? [];
  // limit/offset paging gives no total: a full page implies there may be more.
  const hasNext = records.length === PAGE_SIZE;

  return (
    <section className="admin-audit audit">
      <div className="page-head">
        <h1>{t("page.adminAudit")}</h1>
      </div>

      <div className="audit-filters">
        <label className="field">
          {t("admin.audit.filterCommunity")}
          <select
            value={draft.community}
            onChange={(e) =>
              setDraft((d) => ({ ...d, community: e.target.value }))
            }
          >
            <option value="">{t("admin.audit.filterCommunityAll")}</option>
            {(communities ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <AuditFilterFields
          draft={draft}
          actorError={actorError}
          onChange={(next) => {
            setActorError(false);
            setDraft((d) => ({ ...next, community: d.community }));
          }}
        />
        <button type="button" className="btn primary" onClick={apply}>
          {t("communitySettings.audit.apply")}
        </button>
      </div>

      {query.isPending ? (
        <p className="sub">{t("communitySettings.audit.loading")}</p>
      ) : query.isError ? (
        <p className="field-error">{t("communitySettings.audit.loadError")}</p>
      ) : records.length === 0 ? (
        <p className="sub">{t("communitySettings.audit.empty")}</p>
      ) : (
        <AuditTable records={records} showCommunity />
      )}

      <AuditPaging
        offset={offset}
        hasNext={hasNext}
        isFetching={query.isFetching}
        onPrev={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
        onNext={() => setOffset((o) => o + PAGE_SIZE)}
      />
    </section>
  );
}
