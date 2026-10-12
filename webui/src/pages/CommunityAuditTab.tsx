import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { classifyQueryResult } from "../queryState.ts";
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

const FILTER_KEYS = ["operation", "actor", "since", "until"] as const;

export function auditKey(
  communityId: string,
  filters: AuditFilters,
  offset: number,
) {
  return ["communities", communityId, "audit", filters, offset] as const;
}

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
  // Keep applied filters in the URL and draft edits separate; browser history restores the applied set.
  const [urlFilters, applyFilters] = useFilterParams(FILTER_KEYS);
  const filters: AuditFilters = { ...EMPTY_FILTERS, ...urlFilters };
  const [draft, setDraft] = useState<AuditFilters>(filters);
  const [offset, setOffset] = useOffsetParam();
  const [actorError, setActorError] = useState(false);

  // Use a value signature to resync draft filters only when URL values change.
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
  const queryState = classifyQueryResult(query);

  useEffect(() => {
    if (query.isError) {
      onForbidden(query.error);
    }
  }, [query.isError, query.error, onForbidden]);

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

      {queryState.kind === "pending" ? (
        <p className="sub">{t("communitySettings.audit.loading")}</p>
      ) : /* Retain cached content when only a background refetch fails. */
      queryState.kind === "error" ? (
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
