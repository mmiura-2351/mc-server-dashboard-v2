import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
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
import { useFilterParams, useOffsetParam } from "./urlState.ts";

// The filter keys persisted in the URL query string (#563): the shared
// operation/actor/since/until set (the community-scoped view has no community
// filter — every row is the same community).
const FILTER_KEYS = ["operation", "actor", "since", "until"] as const;

export function auditKey(
  communityId: string,
  filters: AuditFilters,
  offset: number,
) {
  return ["communities", communityId, "audit", filters, offset] as const;
}

// Build the audit-list URL with the applied filters mapped to the exact query
// params the endpoint declares (operation, actor, since, until, limit, offset —
// schema.ts list_community_audit_log). Blank filters are omitted. The result is
// cast back to the template path so the typed response stays AuditLogResponse.
function auditUrl(
  communityId: string,
  filters: AuditFilters,
  offset: number,
): "/api/communities/{community_id}/audit" {
  const base = apiPath("/api/communities/{community_id}/audit", {
    community_id: communityId,
  });
  const params = new URLSearchParams();
  applyAuditParams(params, filters);
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(offset));
  return `${base}?${params.toString()}` as "/api/communities/{community_id}/audit";
}

// Audit tab (WEBUI_SPEC.md 6.10): filterable, paged view of the community audit
// trail. Filters map to the endpoint query params; paging is limit/offset
// (audit.py). Gated by `audit:read`; a 403 from the list routes to onForbidden.
export function CommunityAuditTab({
  communityId,
  can,
}: {
  communityId: string;
  can: Can;
}) {
  if (!can("audit:read")) {
    return <p className="field-error">{t("permissions.denied")}</p>;
  }
  return <Loaded communityId={communityId} />;
}

function Loaded({ communityId }: { communityId: string }) {
  const onForbidden = useOnForbidden();
  // Applied filters live in the URL query string (#563) so reloads and shared
  // links restore them; `urlFilters` is the applied set, `draft` the in-progress
  // input. The draft re-syncs to the URL on Back so the inputs follow history.
  const [urlFilters, applyFilters] = useFilterParams(FILTER_KEYS);
  const filters: AuditFilters = { ...EMPTY_FILTERS, ...urlFilters };
  const [draft, setDraft] = useState<AuditFilters>(filters);
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
    queryKey: auditKey(communityId, filters, offset),
    queryFn: ({ signal }) =>
      api.get(auditUrl(communityId, filters, offset), { signal }),
    placeholderData: keepPreviousData,
  });

  useEffect(() => {
    if (query.isError) {
      onForbidden(query.error);
    }
  }, [query.isError, query.error, onForbidden]);

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
    <section className="audit">
      <div className="page-head">
        <h2>{t("communitySettings.audit.heading")}</h2>
      </div>

      <div className="audit-filters">
        <AuditFilterFields
          draft={draft}
          actorError={actorError}
          onChange={(next) => {
            setActorError(false);
            setDraft(next);
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
        <AuditTable records={records} />
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
