import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { adminCommunitiesPickerKey } from "../api/adminQueryKeys.ts";
import { api } from "../api/client.ts";
import { t } from "../i18n/index.ts";
import {
  AuditFilterFields,
  type AuditFilters,
  AuditPaging,
  AuditTable,
  applyAuditParams,
  isUuid,
  PAGE_SIZE,
} from "./auditShared.tsx";
import { useFilterParams, useOffsetParam } from "./urlState.ts";

// The global view adds a `community` filter (empty = all communities) on top of
// the shared operation/actor/since/until filters.
interface AdminAuditFilters extends AuditFilters {
  community: string;
}

const EMPTY: AdminAuditFilters = {
  operation: "",
  actor: "",
  since: "",
  until: "",
  community: "",
};

// The filter keys persisted in the URL query string (#563); `community` is the
// global view's extra dropdown alongside the shared text/datetime filters.
const FILTER_KEYS = [
  "operation",
  "actor",
  "since",
  "until",
  "community",
] as const;

// The community picker requests one max-size page (the API caps
// /admin/communities at 100). When more communities exist the later ones are
// omitted from the dropdown, so we surface a truncation hint — same pattern as
// the Provision owner picker (#476/#488).
const PICKER_PAGE_SIZE = 100;

// Build the global audit-list URL: the shared filters plus the optional
// `community` param, mapped to the exact query params the endpoint declares
// (community/operation/actor/since/until/limit/offset — schema.ts
// list_audit_log). Blank filters are omitted. The result is cast back to the
// path literal so the typed response stays AuditLogResponse.
function auditUrl(filters: AdminAuditFilters, offset: number): "/api/audit" {
  const params = new URLSearchParams();
  if (filters.community !== "") {
    params.set("community", filters.community);
  }
  applyAuditParams(params, filters);
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(offset));
  return `/api/audit?${params.toString()}` as "/api/audit";
}

// Platform-admin global Audit page (WEBUI_SPEC.md 6.12): the global audit log
// with a community filter added. Same filterable/paged shape as the
// community-scoped tab, plus a community picker and a Community column. The
// route is gated by RequireAdmin (App.tsx); the `/audit` endpoint is guarded by
// platform-admin server-side (audit.py).
export function AdminAuditPage() {
  // The community picker lists ALL communities (admins see every community), not
  // just the admin's own — sourced from the platform-axis GET /admin/communities
  // (#489), the same endpoint the admin Communities page uses. It requests one
  // max-size page; when more communities exist the dropdown is truncated and we
  // surface a hint below.
  const communitiesQuery = useQuery({
    queryKey: adminCommunitiesPickerKey(PICKER_PAGE_SIZE),
    queryFn: () =>
      api.get(
        `/api/admin/communities?limit=${PICKER_PAGE_SIZE}&offset=0` as "/api/admin/communities",
      ),
  });
  const communities = communitiesQuery.data?.communities;
  // The picker shows only the first page; if more communities exist, say so
  // rather than silently omitting the rest (#476/#488).
  const communityTotal = communitiesQuery.data?.total ?? 0;
  const communitiesTruncated =
    communities !== undefined && communityTotal > communities.length;
  // Applied filters live in the URL query string (#563) so reloads and shared
  // links restore them; `urlFilters` is the applied set, `draft` the in-progress
  // input. The draft re-syncs to the URL on Back so the inputs follow history.
  const [urlFilters, applyFilters] = useFilterParams(FILTER_KEYS);
  const filters: AdminAuditFilters = { ...EMPTY, ...urlFilters };
  const [draft, setDraft] = useState<AdminAuditFilters>(filters);
  // Page offset lives in `?offset=N` (#514) so Back restores the prior page.
  const [offset, setOffset] = useOffsetParam();
  const [actorError, setActorError] = useState(false);

  // Keep the inputs in step with the applied (URL) filters: Back/forward and
  // deep links drive the URL, and the draft mirrors it. The signature folds the
  // filter values into one stable dep so the re-sync fires only on a real URL
  // change, not on every render's fresh object.
  const urlSignature = JSON.stringify(urlFilters);
  // biome-ignore lint/correctness/useExhaustiveDependencies: `urlSignature` stands in for the filter values; `filters` is derived from them.
  useEffect(() => {
    setDraft(filters);
  }, [urlSignature]);

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
    // Applying writes the filters to the URL and resets offset to 0 in one
    // history entry (useFilterParams drops the offset param).
    applyFilters(draft);
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
          {communitiesTruncated && communities !== undefined && (
            <div className="hint">
              {t("admin.audit.communitiesTruncatedPrefix")}
              {communities.length}
              {t("admin.audit.communitiesTruncatedMid")}
              {communityTotal}
              {t("admin.audit.communitiesTruncatedSuffix")}
            </div>
          )}
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
        onPrev={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        onNext={() => setOffset(offset + PAGE_SIZE)}
      />
    </section>
  );
}
