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
import { type TranslationKey, t } from "../i18n/index.ts";

type AuditRecordResponse = components["schemas"]["AuditRecordResponse"];

// The audit `operation` codes the backend emits (the `<resource>:<action>`
// constants in api audit/domain/operations.py), mapped to their label keys.
// Kept as an explicit map (the codebase's reason→key convention, e.g.
// AdminUsersPage) so an unmapped/unknown code is a plain `undefined` lookup that
// {@link operationLabel} falls back from — the table never breaks if the backend
// adds a code ahead of this dictionary (#643).
const OPERATION_LABEL_KEY: Record<string, TranslationKey> = {
  "auth:login": "communitySettings.audit.op.auth:login",
  "auth:logout": "communitySettings.audit.op.auth:logout",
  "auth:register": "communitySettings.audit.op.auth:register",
  "auth:refresh": "communitySettings.audit.op.auth:refresh",
  "auth:refresh_reuse": "communitySettings.audit.op.auth:refresh_reuse",
  "auth:session_restore": "communitySettings.audit.op.auth:session_restore",
  "auth:password_change": "communitySettings.audit.op.auth:password_change",
  "auth:profile_update": "communitySettings.audit.op.auth:profile_update",
  "auth:account_delete": "communitySettings.audit.op.auth:account_delete",
  "auth:session_revoke": "communitySettings.audit.op.auth:session_revoke",
  "user:create": "communitySettings.audit.op.user:create",
  "user:deactivate": "communitySettings.audit.op.user:deactivate",
  "user:reactivate": "communitySettings.audit.op.user:reactivate",
  "user:delete": "communitySettings.audit.op.user:delete",
  "user:platform_admin_grant":
    "communitySettings.audit.op.user:platform_admin_grant",
  "user:platform_admin_revoke":
    "communitySettings.audit.op.user:platform_admin_revoke",
  "community:provision": "communitySettings.audit.op.community:provision",
  "community:update": "communitySettings.audit.op.community:update",
  "community:delete": "communitySettings.audit.op.community:delete",
  "member:add": "communitySettings.audit.op.member:add",
  "member:remove": "communitySettings.audit.op.member:remove",
  "role:assign": "communitySettings.audit.op.role:assign",
  "role:unassign": "communitySettings.audit.op.role:unassign",
  "role:create": "communitySettings.audit.op.role:create",
  "role:update": "communitySettings.audit.op.role:update",
  "role:delete": "communitySettings.audit.op.role:delete",
  "grant:create": "communitySettings.audit.op.grant:create",
  "grant:revoke": "communitySettings.audit.op.grant:revoke",
  "server:create": "communitySettings.audit.op.server:create",
  "server:update": "communitySettings.audit.op.server:update",
  "server:delete": "communitySettings.audit.op.server:delete",
  "server:start": "communitySettings.audit.op.server:start",
  "server:stop": "communitySettings.audit.op.server:stop",
  "server:restart": "communitySettings.audit.op.server:restart",
  "server:command": "communitySettings.audit.op.server:command",
  "server:export": "communitySettings.audit.op.server:export",
  "server:import": "communitySettings.audit.op.server:import",
  "backup:create": "communitySettings.audit.op.backup:create",
  "backup:restore": "communitySettings.audit.op.backup:restore",
  "backup:delete": "communitySettings.audit.op.backup:delete",
  "backup:upload": "communitySettings.audit.op.backup:upload",
  "backup:download": "communitySettings.audit.op.backup:download",
  "file:write": "communitySettings.audit.op.file:write",
  "file:rollback": "communitySettings.audit.op.file:rollback",
  "file:upload": "communitySettings.audit.op.file:upload",
  "file:download": "communitySettings.audit.op.file:download",
  "file:rename": "communitySettings.audit.op.file:rename",
  "file:delete": "communitySettings.audit.op.file:delete",
  "file:mkdir": "communitySettings.audit.op.file:mkdir",
  "file:search": "communitySettings.audit.op.file:search",
  "version:refresh": "communitySettings.audit.op.version:refresh",
  "version:jar_gc": "communitySettings.audit.op.version:jar_gc",
  "worker:drain_set": "communitySettings.audit.op.worker:drain_set",
  "worker:drain_clear": "communitySettings.audit.op.worker:drain_clear",
  "group:create": "communitySettings.audit.op.group:create",
  "group:update": "communitySettings.audit.op.group:update",
  "group:delete": "communitySettings.audit.op.group:delete",
  "group:player_add": "communitySettings.audit.op.group:player_add",
  "group:player_remove": "communitySettings.audit.op.group:player_remove",
  "group:attach": "communitySettings.audit.op.group:attach",
  "group:detach": "communitySettings.audit.op.group:detach",
};

// The `target_type` prefixes (TARGET_* in operations.py), mapped to label keys.
const TARGET_TYPE_LABEL_KEY: Record<string, TranslationKey> = {
  community: "communitySettings.audit.targetType.community",
  user: "communitySettings.audit.targetType.user",
  role: "communitySettings.audit.targetType.role",
  grant: "communitySettings.audit.targetType.grant",
  server: "communitySettings.audit.targetType.server",
  backup: "communitySettings.audit.targetType.backup",
  worker: "communitySettings.audit.targetType.worker",
  file: "communitySettings.audit.targetType.file",
  group: "communitySettings.audit.targetType.group",
};

// Human-readable label for an audit `operation` code; an unmapped/unknown code
// falls back to its raw value (#643). Display-only — filtering still uses the
// raw code.
export function operationLabel(operation: string): string {
  const key = OPERATION_LABEL_KEY[operation];
  return key === undefined ? operation : t(key);
}

// Human-readable label for a `target_type` prefix (file/user/server/…); an
// unmapped value falls back to the raw type, mirroring operationLabel (#643).
export function targetTypeLabel(targetType: string): string {
  const key = TARGET_TYPE_LABEL_KEY[targetType];
  return key === undefined ? targetType : t(key);
}

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
  // Raw "type:id" target (type/id alone shown bare), kept verbatim for the hover
  // title; the id is still a raw UUID until id→name resolution lands (#643 needs
  // an API field, deferred). The displayed type prefix is humanized.
  const rawTarget = [entry.target_type, entry.target_id]
    .filter((part) => part !== null)
    .join(":");
  const displayTarget =
    entry.target_type !== null
      ? [targetTypeLabel(entry.target_type), entry.target_id]
          .filter((part) => part !== null)
          .join(": ")
      : rawTarget;
  const actor = entry.actor_id ?? t("communitySettings.audit.systemActor");
  const targetText = rawTarget === "" ? "—" : displayTarget;
  // `title` reveals the full value on hover for the long, often-ellipsizable
  // id/operation/target columns (#519). Hover-only (no keyboard affordance);
  // see #496's a11y posture. The operation/target titles carry the raw code so
  // the underlying value stays discoverable after humanization (#643).
  return (
    <tr>
      <td>{new Date(entry.created_at).toLocaleString()}</td>
      {showCommunity ? (
        <td title={entry.community_id ?? undefined}>
          {entry.community_id ?? "—"}
        </td>
      ) : null}
      <td title={actor}>{actor}</td>
      <td title={entry.operation}>{operationLabel(entry.operation)}</td>
      <td>{entry.outcome}</td>
      <td title={rawTarget === "" ? undefined : rawTarget}>{targetText}</td>
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
