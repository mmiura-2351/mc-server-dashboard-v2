import type { components } from "../api/schema";
import { ResizableTable } from "../components/ResizableColumns.tsx";
import { type TranslationKey, t } from "../i18n/index.ts";

type AuditRecordResponse = components["schemas"]["AuditRecordResponse"];

// Unknown audit operations fall back to raw codes so new backend operations remain displayable.
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
  "plugin:install": "communitySettings.audit.op.plugin:install",
  "plugin:remove": "communitySettings.audit.op.plugin:remove",
  "plugin:enable": "communitySettings.audit.op.plugin:enable",
  "plugin:disable": "communitySettings.audit.op.plugin:disable",
  "plugin:update": "communitySettings.audit.op.plugin:update",
  "plugin:set_side": "communitySettings.audit.op.plugin:set_side",
  "plugin:resolve": "communitySettings.audit.op.plugin:resolve",
};

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
  plugin: "communitySettings.audit.targetType.plugin",
};

// Translate operation labels only; filters keep using raw codes.
export function operationLabel(operation: string): string {
  const key = OPERATION_LABEL_KEY[operation];
  return key === undefined ? operation : t(key);
}

// Preserve unknown target types verbatim.
export function targetTypeLabel(targetType: string): string {
  const key = TARGET_TYPE_LABEL_KEY[targetType];
  return key === undefined ? targetType : t(key);
}

export const PAGE_SIZE = 50;

// Datetime-local inputs are naive local times; convert them to UTC when building API parameters.
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

// Validate actor as a UUID before sending it as an API filter.
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: string): boolean {
  return UUID_RE.test(value);
}

// Omit blank filters and convert local datetime inputs to UTC instants for backend comparisons.
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
    const d = new Date(filters.since);
    if (!Number.isNaN(d.getTime())) {
      params.set("since", d.toISOString());
    }
  }
  if (filters.until !== "") {
    const d = new Date(filters.until);
    if (!Number.isNaN(d.getTime())) {
      params.set("until", d.toISOString());
    }
  }
}

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

export function AuditTable({
  records,
  showCommunity = false,
}: {
  records: AuditRecordResponse[];
  showCommunity?: boolean;
}) {
  // Persist widths separately because the global view's extra column shifts indices.
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
  // Retain raw type:id in the hover title while displaying resolved names and translated types.
  const rawTarget = [entry.target_type, entry.target_id]
    .filter((part) => part !== null)
    .join(":");
  const targetIdPart = entry.target_name ?? entry.target_id;
  const displayTarget =
    entry.target_type !== null
      ? [targetTypeLabel(entry.target_type), targetIdPart]
          .filter((part) => part !== null)
          .join(": ")
      : (targetIdPart ?? rawTarget);
  const actor =
    entry.actor_username ??
    entry.actor_id ??
    t("communitySettings.audit.systemActor");
  // Keep the raw actor ID available on hover; system actors have none.
  const actorTitle = entry.actor_id ?? actor;
  const community = entry.community_name ?? entry.community_id;
  const targetText = rawTarget === "" ? "—" : displayTarget;
  // Keep raw codes and IDs in hover titles after translating labels and resolving names.
  return (
    <tr>
      <td>{new Date(entry.created_at).toLocaleString()}</td>
      {showCommunity ? (
        <td title={entry.community_id ?? undefined}>{community ?? "—"}</td>
      ) : null}
      <td title={actorTitle}>{actor}</td>
      <td title={entry.operation}>{operationLabel(entry.operation)}</td>
      <td>{entry.outcome}</td>
      <td title={rawTarget === "" ? undefined : rawTarget}>{targetText}</td>
    </tr>
  );
}

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
