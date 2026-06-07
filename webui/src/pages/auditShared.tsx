/**
 * Shared audit pieces used by both the community-scoped Audit tab
 * (CommunityAuditTab, WEBUI_SPEC.md 6.10) and the platform-admin global Audit
 * page (AdminAuditPage, WEBUI_SPEC.md 6.12). Both render the same filter inputs,
 * map them to the same query params (operation/actor/since/until + limit/offset),
 * and render the same row shape; the global view adds a `community` filter and a
 * Community column on top. Factored here so the two stay in lockstep without
 * copy-paste (issue #479).
 */

import type { components } from "../api/schema";
import { ResizableTable } from "../components/ResizableColumns.tsx";
import { t } from "../i18n/index.ts";

type AuditRecordResponse = components["schemas"]["AuditRecordResponse"];

// One page; the API caps a single query at 200 and defaults to 50 (audit.py).
export const PAGE_SIZE = 50;

// The text/datetime filters shared by both audit views (the global view adds a
// community filter alongside these). `since`/`until` are naive datetime-local
// wall-clock strings; the caller converts them to UTC instants via
// {@link applyAuditParams}.
export interface AuditFilters {
  operation: string;
  actor: string;
  since: string;
  until: string;
}

export const EMPTY_FILTERS: AuditFilters = {
  operation: "",
  actor: "",
  since: "",
  until: "",
};

// A v1-v5 UUID, the shape the backend's `actor` query param parses.
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: string): boolean {
  return UUID_RE.test(value);
}

// Map the applied text/datetime filters onto the shared query params both audit
// endpoints declare. Blank filters are omitted. The datetime-local inputs are
// naive local wall-clock; the backend compares against tz-aware UTC, so the
// local instant is converted to a UTC ISO string.
export function applyAuditParams(
  params: URLSearchParams,
  filters: AuditFilters,
): void {
  if (filters.operation.trim() !== "") {
    params.set("operation", filters.operation.trim());
  }
  if (filters.actor.trim() !== "") {
    params.set("actor", filters.actor.trim());
  }
  if (filters.since !== "") {
    params.set("since", new Date(filters.since).toISOString());
  }
  if (filters.until !== "") {
    params.set("until", new Date(filters.until).toISOString());
  }
}

// The operation/actor/since/until inputs shared by both views. The community
// filter (global view only) is rendered by the caller before these. Strings come
// from the communitySettings.audit.* dictionary, reused across both views.
export function AuditFilterFields({
  draft,
  actorError,
  onChange,
}: {
  draft: AuditFilters;
  actorError: boolean;
  onChange: (next: AuditFilters) => void;
}) {
  return (
    <>
      <label className="field">
        {t("communitySettings.audit.filterOperation")}
        <input
          type="text"
          value={draft.operation}
          placeholder={t("communitySettings.audit.filterOperationPlaceholder")}
          onChange={(e) => onChange({ ...draft, operation: e.target.value })}
        />
      </label>
      <label className="field">
        {t("communitySettings.audit.filterActor")}
        <input
          type="text"
          value={draft.actor}
          placeholder={t("communitySettings.audit.filterActorPlaceholder")}
          onChange={(e) => onChange({ ...draft, actor: e.target.value })}
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
          onChange={(e) => onChange({ ...draft, since: e.target.value })}
        />
      </label>
      <label className="field">
        {t("communitySettings.audit.filterUntil")}
        <input
          type="datetime-local"
          value={draft.until}
          onChange={(e) => onChange({ ...draft, until: e.target.value })}
        />
      </label>
    </>
  );
}

// The audit-record table. The global view passes `showCommunity` to add a
// Community column (the community-scoped view omits it: every row is the same
// community).
export function AuditTable({
  records,
  showCommunity = false,
}: {
  records: AuditRecordResponse[];
  showCommunity?: boolean;
}) {
  // Keyed per view: the global view's extra Community column shifts column
  // indices, so the two persist widths independently.
  const storageKey = showCommunity
    ? "mcsd.colw.audit-global"
    : "mcsd.colw.audit";
  return (
    <ResizableTable storageKey={storageKey} className="data">
      <thead>
        <tr>
          <th>{t("communitySettings.audit.colTime")}</th>
          {showCommunity ? <th>{t("admin.audit.colCommunity")}</th> : null}
          <th>{t("communitySettings.audit.colActor")}</th>
          <th>{t("communitySettings.audit.colOperation")}</th>
          <th>{t("communitySettings.audit.colOutcome")}</th>
          <th>{t("communitySettings.audit.colTarget")}</th>
        </tr>
      </thead>
      <tbody>
        {records.map((entry) => (
          <AuditRow
            key={entry.id}
            entry={entry}
            showCommunity={showCommunity}
          />
        ))}
      </tbody>
    </ResizableTable>
  );
}

function AuditRow({
  entry,
  showCommunity,
}: {
  entry: AuditRecordResponse;
  showCommunity: boolean;
}) {
  // Target is "type:id" when both are present; either alone is shown bare.
  const target = [entry.target_type, entry.target_id]
    .filter((part) => part !== null)
    .join(":");
  const actor = entry.actor_id ?? t("communitySettings.audit.systemActor");
  const targetText = target === "" ? "—" : target;
  // `title` reveals the full value on hover for the long, often-ellipsizable
  // id/operation/target columns (#519). Hover-only (no keyboard affordance);
  // see #496's a11y posture.
  return (
    <tr>
      <td>{new Date(entry.created_at).toLocaleString()}</td>
      {showCommunity ? (
        <td title={entry.community_id ?? undefined}>
          {entry.community_id ?? "—"}
        </td>
      ) : null}
      <td title={actor}>{actor}</td>
      <td title={entry.operation}>{entry.operation}</td>
      <td>{entry.outcome}</td>
      <td title={target === "" ? undefined : target}>{targetText}</td>
    </tr>
  );
}

// Prev/Next paging over the limit/offset window (no total: a full page implies
// there may be more). Shared by both views.
export function AuditPaging({
  offset,
  hasNext,
  isFetching,
  onPrev,
  onNext,
}: {
  offset: number;
  hasNext: boolean;
  isFetching: boolean;
  onPrev: () => void;
  onNext: () => void;
}) {
  return (
    <div className="audit-paging">
      <button
        type="button"
        className="btn sm ghost"
        disabled={offset === 0 || isFetching}
        onClick={onPrev}
      >
        {t("communitySettings.audit.prev")}
      </button>
      <button
        type="button"
        className="btn sm ghost"
        disabled={!hasNext || isFetching}
        onClick={onNext}
      >
        {t("communitySettings.audit.next")}
      </button>
    </div>
  );
}
