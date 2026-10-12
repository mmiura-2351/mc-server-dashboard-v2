import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { adminCommunitiesPickerKey } from "../api/adminQueryKeys.ts";
import { api } from "../api/client.ts";
import { t } from "../i18n/index.ts";
import { classifyQueryResult } from "../queryState.ts";
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

const FILTER_KEYS = [
  "operation",
  "actor",
  "since",
  "until",
  "community",
] as const;

// The API caps the picker at 100 communities; show when the list is truncated.
const PICKER_PAGE_SIZE = 100;

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

export function AdminAuditPage() {
  // Use the admin-wide list; the membership-scoped switcher would omit other communities.
  const communitiesQuery = useQuery({
    queryKey: adminCommunitiesPickerKey(PICKER_PAGE_SIZE),
    queryFn: ({ signal }) =>
      api.get(
        `/api/admin/communities?limit=${PICKER_PAGE_SIZE}&offset=0` as "/api/admin/communities",
        { signal },
      ),
  });
  const communities = communitiesQuery.data?.communities;
  const communityTotal = communitiesQuery.data?.total ?? 0;
  const communitiesTruncated =
    communities !== undefined && communityTotal > communities.length;
  // Keep applied filters in the URL and draft edits separate; browser history restores the applied set.
  const [urlFilters, applyFilters] = useFilterParams(FILTER_KEYS);
  const filters: AdminAuditFilters = { ...EMPTY, ...urlFilters };
  const [draft, setDraft] = useState<AdminAuditFilters>(filters);
  const [offset, setOffset] = useOffsetParam();
  const [actorError, setActorError] = useState(false);

  // Use a value signature to resync draft filters only when URL values change.
  const urlSignature = JSON.stringify(urlFilters);
  // biome-ignore lint/correctness/useExhaustiveDependencies: `urlSignature` stands in for the filter values; `filters` is derived from them.
  useEffect(() => {
    setDraft(filters);
  }, [urlSignature]);

  const query = useQuery({
    queryKey: ["admin", "audit", filters, offset],
    queryFn: ({ signal }) => api.get(auditUrl(filters, offset), { signal }),
    placeholderData: keepPreviousData,
  });
  const queryState = classifyQueryResult(query);

  const apply = () => {
    // Reject non-UUID actors inline instead of turning an API 422 into a generic load error.
    if (draft.actor.trim() !== "" && !isUuid(draft.actor.trim())) {
      setActorError(true);
      return;
    }
    setActorError(false);
    applyFilters(draft);
  };

  const records = query.data?.records ?? [];
  // The API returns no total; a full page only suggests that another page may exist.
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
              {t("admin.audit.communitiesTruncated", {
                n: communities.length,
                total: communityTotal,
              })}
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

      {queryState.kind === "pending" ? (
        <p className="sub">{t("communitySettings.audit.loading")}</p>
      ) : /* Retain cached content when only a background refetch fails. */
      queryState.kind === "error" ? (
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
