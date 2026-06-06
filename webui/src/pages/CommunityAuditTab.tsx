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
): "/communities/{community_id}/audit" {
  const base = apiPath("/communities/{community_id}/audit", {
    community_id: communityId,
  });
  const params = new URLSearchParams();
  applyAuditParams(params, filters);
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(offset));
  return `${base}?${params.toString()}` as "/communities/{community_id}/audit";
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
  // Applied filters (committed on Apply) and the in-progress input draft.
  const [filters, setFilters] = useState<AuditFilters>(EMPTY_FILTERS);
  const [draft, setDraft] = useState<AuditFilters>(EMPTY_FILTERS);
  const [offset, setOffset] = useState(0);
  const [actorError, setActorError] = useState(false);

  const query = useQuery({
    queryKey: auditKey(communityId, filters, offset),
    queryFn: () => api.get(auditUrl(communityId, filters, offset)),
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
    setFilters(draft);
    setOffset(0);
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
        onPrev={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
        onNext={() => setOffset((o) => o + PAGE_SIZE)}
      />
    </section>
  );
}
