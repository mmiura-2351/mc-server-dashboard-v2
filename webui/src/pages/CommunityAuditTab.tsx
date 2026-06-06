import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";

type AuditRecordResponse = components["schemas"]["AuditRecordResponse"];

// One page; the API caps a single query at 200 and defaults to 50 (audit.py).
const PAGE_SIZE = 50;

// The filters the caller has applied (Apply commits the inputs into a query).
interface AuditFilters {
  operation: string;
  actor: string;
  since: string;
  until: string;
}

const EMPTY_FILTERS: AuditFilters = {
  operation: "",
  actor: "",
  since: "",
  until: "",
};

export function auditKey(
  communityId: string,
  filters: AuditFilters,
  offset: number,
) {
  return ["communities", communityId, "audit", filters, offset] as const;
}

// A v1-v5 UUID, the shape the backend's `actor` query param parses.
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function isUuid(value: string): boolean {
  return UUID_RE.test(value);
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
  if (filters.operation.trim() !== "") {
    params.set("operation", filters.operation.trim());
  }
  if (filters.actor.trim() !== "") {
    params.set("actor", filters.actor.trim());
  }
  // The datetime-local inputs are naive local wall-clock; the backend compares
  // against tz-aware UTC, so convert the local instant to a UTC ISO string.
  if (filters.since !== "") {
    params.set("since", new Date(filters.since).toISOString());
  }
  if (filters.until !== "") {
    params.set("until", new Date(filters.until).toISOString());
  }
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
        <label className="field">
          {t("communitySettings.audit.filterOperation")}
          <input
            type="text"
            value={draft.operation}
            placeholder={t(
              "communitySettings.audit.filterOperationPlaceholder",
            )}
            onChange={(e) =>
              setDraft((d) => ({ ...d, operation: e.target.value }))
            }
          />
        </label>
        <label className="field">
          {t("communitySettings.audit.filterActor")}
          <input
            type="text"
            value={draft.actor}
            placeholder={t("communitySettings.audit.filterActorPlaceholder")}
            onChange={(e) => {
              setActorError(false);
              setDraft((d) => ({ ...d, actor: e.target.value }));
            }}
          />
          {actorError ? (
            <span className="field-error">
              {t("communitySettings.audit.filterActorInvalid")}
            </span>
          ) : null}
        </label>
        <label className="field">
          {t("communitySettings.audit.filterSince")}
          <input
            type="datetime-local"
            value={draft.since}
            onChange={(e) => setDraft((d) => ({ ...d, since: e.target.value }))}
          />
        </label>
        <label className="field">
          {t("communitySettings.audit.filterUntil")}
          <input
            type="datetime-local"
            value={draft.until}
            onChange={(e) => setDraft((d) => ({ ...d, until: e.target.value }))}
          />
        </label>
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
        <table className="data">
          <thead>
            <tr>
              <th>{t("communitySettings.audit.colTime")}</th>
              <th>{t("communitySettings.audit.colActor")}</th>
              <th>{t("communitySettings.audit.colOperation")}</th>
              <th>{t("communitySettings.audit.colOutcome")}</th>
              <th>{t("communitySettings.audit.colTarget")}</th>
            </tr>
          </thead>
          <tbody>
            {records.map((entry) => (
              <AuditRow key={entry.id} entry={entry} />
            ))}
          </tbody>
        </table>
      )}

      <div className="audit-paging">
        <button
          type="button"
          className="btn sm ghost"
          disabled={offset === 0 || query.isFetching}
          onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
        >
          {t("communitySettings.audit.prev")}
        </button>
        <button
          type="button"
          className="btn sm ghost"
          disabled={!hasNext || query.isFetching}
          onClick={() => setOffset((o) => o + PAGE_SIZE)}
        >
          {t("communitySettings.audit.next")}
        </button>
      </div>
    </section>
  );
}

function AuditRow({ entry }: { entry: AuditRecordResponse }) {
  // Target is "type:id" when both are present; either alone is shown bare.
  const target = [entry.target_type, entry.target_id]
    .filter((part) => part !== null)
    .join(":");
  return (
    <tr>
      <td>{new Date(entry.created_at).toLocaleString()}</td>
      <td>{entry.actor_id ?? t("communitySettings.audit.systemActor")}</td>
      <td>{entry.operation}</td>
      <td>{entry.outcome}</td>
      <td>{target === "" ? "—" : target}</td>
    </tr>
  );
}
