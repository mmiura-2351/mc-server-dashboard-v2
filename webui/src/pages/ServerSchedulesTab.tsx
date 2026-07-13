/**
 * Server detail — Schedules tab (WEBUI_SPEC.md 6.13, epic #649 / issue #1842).
 *
 * The Web UI for the general scheduler (#1837): a per-server table of schedules
 * (name / action / cadence / timezone / enabled / last-run / next-run), a
 * create-edit dialog, an enable/disable toggle, delete, and a run-history view.
 *
 * Authorization is two-layer and write-time only: reads need `schedule:read`;
 * writes need `schedule:manage` **and** the action's own permission
 * (`command`→`server:command`, `start/stop/restart`→`server:{start,stop,restart}`,
 * `backup`→`backup:schedule`). The create dialog offers only the actions the
 * caller may run; edit/toggle/delete of an existing row require the same gate on
 * that row's action. The action is immutable — to change it, delete and recreate
 * (so the action select is disabled when editing). Warning steps (stop/restart
 * only) broadcast a fixed `say`, so they need no extra permission.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { fieldErrorsFromValidation } from "../api/validationErrors.ts";
import { Modal } from "../components/Modal.tsx";
import { ResizableTable } from "../components/ResizableColumns.tsx";
import { useToast } from "../components/Toast.tsx";
import { formatDateTime } from "../format.ts";
import { getLanguage, type TranslationKey, t } from "../i18n/index.ts";
import type { PermissionCode } from "../permissions/catalog.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";

type ServerResponse = components["schemas"]["ServerResponse"];
type ScheduleResponse = components["schemas"]["ScheduleResponse"];
type ScheduleRunResponse = components["schemas"]["ScheduleRunResponse"];
type ScheduleAction = components["schemas"]["ScheduleAction"];
type CreateScheduleRequest = components["schemas"]["CreateScheduleRequest"];
type UpdateScheduleRequest = components["schemas"]["UpdateScheduleRequest"];

const ACTIONS: readonly ScheduleAction[] = [
  "command",
  "start",
  "stop",
  "restart",
  "backup",
];

/** The action's own permission, the Layer-2 half of the write gate (#1837). */
const ACTION_PERMISSION: Record<ScheduleAction, PermissionCode> = {
  command: "server:command",
  start: "server:start",
  stop: "server:stop",
  restart: "server:restart",
  backup: "backup:schedule",
};

/** Actions whose pre-action player warnings are editable (#1839). */
const WARNING_ACTIONS: ReadonlySet<ScheduleAction> = new Set([
  "stop",
  "restart",
]);

const MAX_WARNING_STEPS = 5;
const MIN_OFFSET_MINUTES = 1;
const MAX_OFFSET_MINUTES = 120;

const ACTION_LABEL: Record<ScheduleAction, TranslationKey> = {
  command: "schedules.action.command",
  start: "schedules.action.start",
  stop: "schedules.action.stop",
  restart: "schedules.action.restart",
  backup: "schedules.action.backup",
};

const OUTCOME_LABEL: Record<string, TranslationKey> = {
  success: "schedules.runs.outcome.success",
  failure: "schedules.runs.outcome.failure",
  skipped: "schedules.runs.outcome.skipped",
};

/** Curated short list of major IANA zones for the timezone select. */
function supportedTimeZones(): readonly string[] {
  return [
    "UTC",
    "Asia/Tokyo",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Berlin",
  ];
}

const TIMEZONES = supportedTimeZones();

/** Schedules list query key — scoped to the server, invalidated on change. */
function schedulesKey(communityId: string, serverId: string) {
  return ["schedules", communityId, serverId] as const;
}

/** Run-history query key for one schedule. */
function runsKey(communityId: string, serverId: string, scheduleId: string) {
  return ["schedules", communityId, serverId, scheduleId, "runs"] as const;
}

/**
 * Day-of-week labels keyed by ISO number (1=Mon, 7=Sun) for both the builder
 * UI and the humanized cadence display.
 */
const DAY_LABELS: Record<number, TranslationKey> = {
  1: "schedules.dialog.day.mon",
  2: "schedules.dialog.day.tue",
  3: "schedules.dialog.day.wed",
  4: "schedules.dialog.day.thu",
  5: "schedules.dialog.day.fri",
  6: "schedules.dialog.day.sat",
  7: "schedules.dialog.day.sun",
};

/** ISO day numbers in display order (Mon–Sun). */
const DAY_NUMBERS: readonly number[] = [1, 2, 3, 4, 5, 6, 7];

/**
 * Parse a cron expression that matches the Daily/Weekly pattern (`M H * * D`
 * where D is `*` or a comma-separated list of 0–7 day numbers). Returns null
 * for unrecognized patterns. Handles both 0 and 7 as Sunday.
 */
function parseDailyWeeklyCron(
  expr: string,
): { minute: number; hour: number; days: number[] | null } | null {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minuteStr, hourStr, dom, month, dow] = parts;
  if (dom !== "*" || month !== "*") return null;
  const minute = Number(minuteStr);
  const hour = Number(hourStr);
  if (!Number.isInteger(minute) || minute < 0 || minute > 59) return null;
  if (!Number.isInteger(hour) || hour < 0 || hour > 23) return null;
  if (dow === "*") {
    return { minute, hour, days: null }; // every day
  }
  // Parse comma-separated day numbers (0–7, where 0 and 7 are Sunday).
  const dayStrs = dow.split(",");
  const days: number[] = [];
  for (const d of dayStrs) {
    const n = Number(d);
    if (!Number.isInteger(n) || n < 0 || n > 7) return null;
    // Normalize: 0 → 7 (Sunday in ISO)
    days.push(n === 0 ? 7 : n);
  }
  // Deduplicate and sort (ISO order).
  const unique = [...new Set(days)].sort((a, b) => a - b);
  return { minute, hour, days: unique };
}

/** Compose a cron expression from the Daily/Weekly builder state. */
function composeDailyWeeklyCron(
  hour: number,
  minute: number,
  days: number[] | null,
): string {
  const dow = days === null || days.length === 7 ? "*" : days.join(",");
  return `${minute} ${hour} * * ${dow}`;
}

/** Join a list of strings with locale-aware separators (e.g. ", " for en, "、" for ja). */
function joinDayNames(names: string[]): string {
  try {
    return new Intl.ListFormat(getLanguage(), { type: "conjunction" }).format(
      names,
    );
  } catch {
    return names.join(", ");
  }
}

/** Format hour:minute as HH:MM. */
function formatTime(hour: number, minute: number): string {
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

/** Human-readable cadence: an interval as "every N …", or the cron expression. */
function humanizeCadence(schedule: ScheduleResponse): string {
  if (schedule.cron !== null) {
    const parsed = parseDailyWeeklyCron(schedule.cron);
    if (parsed !== null) {
      const time = formatTime(parsed.hour, parsed.minute);
      if (parsed.days === null) {
        return t("schedules.cadence.dailyAt", { time });
      }
      const dayNames = joinDayNames(parsed.days.map((d) => t(DAY_LABELS[d])));
      return t("schedules.cadence.daysAt", { days: dayNames, time });
    }
    return t("schedules.cadence.cron", { cron: schedule.cron });
  }
  if (schedule.interval_seconds !== null) {
    const seconds = schedule.interval_seconds;
    if (seconds % 3600 === 0) {
      return t("schedules.cadence.everyHours", { count: seconds / 3600 });
    }
    if (seconds % 60 === 0) {
      return t("schedules.cadence.everyMinutes", { count: seconds / 60 });
    }
    return t("schedules.cadence.everySeconds", { count: seconds });
  }
  return t("schedules.none");
}

export function ServerSchedulesTab({
  server,
  communityId,
  can,
}: {
  server: ServerResponse;
  communityId: string;
  can: Can;
}) {
  const serverId = server.id;
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<
    { mode: "create" } | { mode: "edit"; schedule: ScheduleResponse } | null
  >(null);
  const [deleteTarget, setDeleteTarget] = useState<ScheduleResponse | null>(
    null,
  );
  const [historyTarget, setHistoryTarget] = useState<ScheduleResponse | null>(
    null,
  );

  const canRead = can("schedule:read", { serverId });
  const canManage = can("schedule:manage", { serverId });
  const canAction = (action: ScheduleAction) =>
    can(ACTION_PERMISSION[action], { serverId });
  // The create dialog offers only actions the caller may run (anti-escalation),
  // so a schedule:manage holder with no action permission cannot create at all.
  const permittedActions = ACTIONS.filter(canAction);
  const canCreate = canManage && permittedActions.length > 0;

  const listQuery = useQuery({
    queryKey: schedulesKey(communityId, serverId),
    enabled: canRead,
    queryFn: ({ signal }) =>
      api.get(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/schedules",
          { community_id: communityId, server_id: serverId },
        ),
        { signal },
      ),
  });

  const refresh = () => {
    queryClient.invalidateQueries({
      queryKey: schedulesKey(communityId, serverId),
    });
  };

  const onError = (error: unknown) => {
    if (onForbidden(error)) {
      return;
    }
    showToast(t("schedules.error.generic"), "error");
  };

  const toggle = useMutation({
    mutationFn: (schedule: ScheduleResponse) =>
      api.patch(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/schedules/{schedule_id}",
          {
            community_id: communityId,
            server_id: serverId,
            schedule_id: schedule.id,
          },
        ),
        { body: JSON.stringify({ enabled: !schedule.enabled }) },
      ),
    onSuccess: (_data, schedule) => {
      showToast(
        t(
          schedule.enabled
            ? "schedules.toggle.disabled"
            : "schedules.toggle.enabled",
        ),
        "success",
      );
      refresh();
    },
    onError,
  });

  const remove = useMutation({
    mutationFn: (schedule: ScheduleResponse) =>
      api.delete(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/schedules/{schedule_id}",
          {
            community_id: communityId,
            server_id: serverId,
            schedule_id: schedule.id,
          },
        ),
      ),
    onSuccess: () => {
      showToast(t("schedules.deleted"), "success");
      refresh();
    },
    onError,
  });

  if (!canRead) {
    return <p className="sub">{t("schedules.noRead")}</p>;
  }
  if (listQuery.isPending) {
    return <p className="sub">{t("schedules.loading")}</p>;
  }
  // Error only when there is nothing to show (an initial load failed); a failed
  // background refetch retains `data` so the cached page keeps rendering (#1805).
  if (listQuery.data === undefined) {
    return <p className="field-error">{t("schedules.loadError")}</p>;
  }

  const schedules = listQuery.data;

  return (
    <section className="schedules">
      {canCreate && (
        <div className="schedules-toolbar">
          <button
            type="button"
            className="btn primary"
            onClick={() => setDialog({ mode: "create" })}
          >
            {t("schedules.create")}
          </button>
        </div>
      )}

      <div className="card schedules-table">
        <ResizableTable storageKey="mcsd.colw.schedules" className="data">
          <thead>
            <tr>
              <th>{t("schedules.col.name")}</th>
              <th>{t("schedules.col.action")}</th>
              <th>{t("schedules.col.cadence")}</th>
              <th>{t("schedules.col.timezone")}</th>
              <th>{t("schedules.col.enabled")}</th>
              <th>{t("schedules.col.lastRun")}</th>
              <th>{t("schedules.col.nextRun")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {schedules.length === 0 ? (
              <tr>
                <td colSpan={8} className="sub">
                  {t("schedules.empty")}
                </td>
              </tr>
            ) : (
              schedules.map((schedule) => {
                const action = schedule.action as ScheduleAction;
                const canWrite = canManage && canAction(action);
                // Fall back to the raw value for an action outside the known
                // enum, mirroring the run-outcome badge.
                const actionLabel = ACTION_LABEL[action];
                return (
                  <tr key={schedule.id}>
                    <td>{schedule.name}</td>
                    <td>
                      <span className="badge">
                        {actionLabel !== undefined
                          ? t(actionLabel)
                          : schedule.action}
                      </span>
                    </td>
                    <td>{humanizeCadence(schedule)}</td>
                    <td>{schedule.timezone}</td>
                    <td>
                      <input
                        type="checkbox"
                        checked={schedule.enabled}
                        disabled={!canWrite || toggle.isPending}
                        aria-label={t("schedules.enabledLabel", {
                          name: schedule.name,
                        })}
                        onChange={() => toggle.mutate(schedule)}
                      />
                    </td>
                    <td>
                      {schedule.last_run_at !== null
                        ? formatDateTime(schedule.last_run_at)
                        : t("schedules.none")}
                    </td>
                    <td>
                      {schedule.next_run_at !== null
                        ? formatDateTime(schedule.next_run_at)
                        : t("schedules.none")}
                    </td>
                    <td className="row-actions">
                      <button
                        type="button"
                        className="btn sm"
                        onClick={() => setHistoryTarget(schedule)}
                      >
                        {t("schedules.history")}
                      </button>
                      {canWrite && (
                        <>
                          <button
                            type="button"
                            className="btn sm"
                            onClick={() =>
                              setDialog({ mode: "edit", schedule })
                            }
                          >
                            {t("schedules.edit")}
                          </button>
                          <button
                            type="button"
                            className="btn sm danger"
                            onClick={() => setDeleteTarget(schedule)}
                          >
                            {t("schedules.delete")}
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </ResizableTable>
      </div>

      {dialog !== null && (
        <ScheduleDialog
          communityId={communityId}
          serverId={serverId}
          permittedActions={permittedActions}
          existing={dialog.mode === "edit" ? dialog.schedule : null}
          onDone={refresh}
          onClose={() => setDialog(null)}
        />
      )}

      {deleteTarget !== null && (
        <Modal
          open={true}
          title={t("schedules.deleteDialog.title")}
          onClose={() => setDeleteTarget(null)}
          footer={
            <>
              <button
                type="button"
                className="btn ghost"
                onClick={() => setDeleteTarget(null)}
              >
                {t("common.cancel")}
              </button>
              <button
                type="button"
                className="btn danger"
                disabled={remove.isPending}
                onClick={() => {
                  const target = deleteTarget;
                  setDeleteTarget(null);
                  remove.mutate(target);
                }}
              >
                {t("schedules.deleteDialog.confirm")}
              </button>
            </>
          }
        >
          <p>{t("schedules.deleteDialog.body", { name: deleteTarget.name })}</p>
        </Modal>
      )}

      {historyTarget !== null && (
        <RunHistoryDialog
          communityId={communityId}
          serverId={serverId}
          schedule={historyTarget}
          onClose={() => setHistoryTarget(null)}
        />
      )}
    </section>
  );
}

interface WarningStepForm {
  offset: string;
  message: string;
}

/**
 * Create/edit dialog. On create the action select is gated to the permitted
 * actions; on edit it is disabled (the action is immutable — delete and
 * recreate to change it). The cadence is a `cron` XOR `interval` choice; the
 * command line shows only for `command`; the warning-steps editor only for
 * `stop`/`restart`. Server validation errors map to inline field messages.
 */
function ScheduleDialog({
  communityId,
  serverId,
  permittedActions,
  existing,
  onDone,
  onClose,
}: {
  communityId: string;
  serverId: string;
  permittedActions: readonly ScheduleAction[];
  existing: ScheduleResponse | null;
  onDone: () => void;
  onClose: () => void;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const editing = existing !== null;

  const initialAction: ScheduleAction = existing
    ? (existing.action as ScheduleAction)
    : (permittedActions[0] ?? "backup");
  const [action, setAction] = useState<ScheduleAction>(initialAction);
  const [name, setName] = useState(existing?.name ?? "");
  // Detect the initial cadence mode: if the existing cron matches the
  // Daily/Weekly pattern, open in that mode; otherwise raw cron or interval.
  const existingParsed =
    existing?.cron != null ? parseDailyWeeklyCron(existing.cron) : null;
  const [cadenceMode, setCadenceMode] = useState<
    "interval" | "dailyWeekly" | "cron"
  >(
    existingParsed !== null
      ? "dailyWeekly"
      : existing?.cron !== null && existing?.cron !== undefined
        ? "cron"
        : "interval",
  );
  const initialInterval =
    existing?.interval_seconds != null
      ? intervalToForm(existing.interval_seconds)
      : { value: "60", unit: "minutes" as const };
  const [intervalValue, setIntervalValue] = useState(initialInterval.value);
  const [intervalUnit, setIntervalUnit] = useState<"minutes" | "hours">(
    initialInterval.unit,
  );
  const [cron, setCron] = useState(existing?.cron ?? "");
  // Daily/Weekly builder state.
  const [dwRepeat, setDwRepeat] = useState<"everyDay" | "specificDays">(
    existingParsed?.days != null ? "specificDays" : "everyDay",
  );
  const [dwDays, setDwDays] = useState<Set<number>>(
    new Set(existingParsed?.days ?? []),
  );
  const [dwHour, setDwHour] = useState(String(existingParsed?.hour ?? 0));
  const [dwMinute, setDwMinute] = useState(String(existingParsed?.minute ?? 0));
  const [timezone, setTimezone] = useState(existing?.timezone ?? "UTC");
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [command, setCommand] = useState(existing?.command ?? "");
  const [warnings, setWarnings] = useState<WarningStepForm[]>(
    (existing?.warning_steps ?? []).map((step) => ({
      offset: String(step.offset_minutes),
      message: step.message,
    })),
  );

  const [nameError, setNameError] = useState<string | null>(null);
  const [cadenceError, setCadenceError] = useState<string | null>(null);
  const [timezoneError, setTimezoneError] = useState<string | null>(null);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [warningError, setWarningError] = useState<string | null>(null);

  const showWarnings = WARNING_ACTIONS.has(action);
  const noDaysSelected =
    cadenceMode === "dailyWeekly" &&
    dwRepeat === "specificDays" &&
    dwDays.size === 0;
  // On create an unusual existing timezone can never occur; on edit surface the
  // stored value even if the runtime zone list omits it.
  const tzOptions = TIMEZONES.includes(timezone)
    ? TIMEZONES
    : [timezone, ...TIMEZONES];

  const clearErrors = () => {
    setNameError(null);
    setCadenceError(null);
    setTimezoneError(null);
    setCommandError(null);
    setWarningError(null);
  };

  const buildCadence = (): {
    cron: string | null;
    interval_seconds: number | null;
  } => {
    if (cadenceMode === "cron") {
      return { cron: cron.trim(), interval_seconds: null };
    }
    if (cadenceMode === "dailyWeekly") {
      const h = Number(dwHour);
      const m = Number(dwMinute);
      const days =
        dwRepeat === "everyDay" ? null : [...dwDays].sort((a, b) => a - b);
      return {
        cron: composeDailyWeeklyCron(h, m, days),
        interval_seconds: null,
      };
    }
    const factor = intervalUnit === "hours" ? 3600 : 60;
    return {
      cron: null,
      interval_seconds: Math.round(Number(intervalValue) * factor),
    };
  };

  const warningSteps = () =>
    warnings.map((step) => ({
      offset_minutes: Number(step.offset),
      message: step.message,
    }));

  const onMutationError = (error: unknown) => {
    if (onForbidden(error)) {
      onClose();
      return;
    }
    if (!(error instanceof ApiError)) {
      showToast(t("schedules.error.generic"), "error");
      return;
    }
    switch (error.reason) {
      case "invalid_cron":
        setCadenceError(t("schedules.error.invalidCron"));
        return;
      case "invalid_cadence":
        setCadenceError(t("schedules.error.invalidCadence"));
        return;
      case "invalid_timezone":
        setTimezoneError(t("schedules.error.invalidTimezone"));
        return;
      case "invalid_schedule_name":
        setNameError(t("schedules.error.invalidName"));
        return;
      case "schedule_name_exists":
        setNameError(t("schedules.error.nameExists"));
        return;
      case "invalid_payload":
        if (showWarnings) {
          setWarningError(t("schedules.error.invalidWarnings"));
          return;
        }
        if (action === "command") {
          setCommandError(t("schedules.error.invalidCommand"));
          return;
        }
        // Neither payload field is rendered for this action (start/backup):
        // fall through to the generic toast rather than an invisible error.
        break;
      case "validation_error": {
        const fields = fieldErrorsFromValidation(error.body, [
          "name",
          "command",
        ]);
        if (fields?.name !== undefined) {
          setNameError(fields.name);
          return;
        }
        if (fields?.command !== undefined) {
          setCommandError(fields.command);
          return;
        }
        break;
      }
    }
    showToast(t("schedules.error.generic"), "error");
  };

  const save = useMutation({
    mutationFn: () => {
      const cadence = buildCadence();
      if (editing) {
        const body: UpdateScheduleRequest = {
          name,
          timezone,
          enabled,
          cron: cadence.cron,
          interval_seconds: cadence.interval_seconds,
          command: action === "command" ? command : null,
          warning_steps: showWarnings ? warningSteps() : null,
        };
        return api.patch(
          apiPath(
            "/api/communities/{community_id}/servers/{server_id}/schedules/{schedule_id}",
            {
              community_id: communityId,
              server_id: serverId,
              schedule_id: existing.id,
            },
          ),
          { body: JSON.stringify(body) },
        );
      }
      const body: CreateScheduleRequest = {
        name,
        action,
        timezone,
        enabled,
        cron: cadence.cron,
        interval_seconds: cadence.interval_seconds,
        command: action === "command" ? command : null,
        warning_steps: showWarnings ? warningSteps() : null,
      };
      return api.post(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/schedules",
          { community_id: communityId, server_id: serverId },
        ),
        { body: JSON.stringify(body) },
      );
    },
    onSuccess: () => {
      showToast(
        t(editing ? "schedules.updated" : "schedules.created"),
        "success",
      );
      onDone();
      onClose();
    },
    onError: onMutationError,
  });

  const submit = () => {
    clearErrors();
    // Client-side floor: the API rejects interval_seconds < 60 (as a generic
    // invalid_cadence), and the number input's min does not block a typed
    // fractional/zero value — catch it here with a specific message.
    if (cadenceMode === "interval") {
      const seconds = buildCadence().interval_seconds;
      if (seconds === null || !Number.isFinite(seconds) || seconds < 60) {
        setCadenceError(t("schedules.error.intervalTooShort"));
        return;
      }
    }
    save.mutate();
  };

  const addWarning = () =>
    setWarnings((prev) =>
      prev.length >= MAX_WARNING_STEPS
        ? prev
        : [...prev, { offset: "5", message: "" }],
    );
  const removeWarning = (index: number) =>
    setWarnings((prev) => prev.filter((_, i) => i !== index));
  const updateWarning = (index: number, patch: Partial<WarningStepForm>) =>
    setWarnings((prev) =>
      prev.map((step, i) => (i === index ? { ...step, ...patch } : step)),
    );

  return (
    <Modal
      open={true}
      title={t(
        editing ? "schedules.dialog.editTitle" : "schedules.dialog.createTitle",
      )}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={save.isPending || noDaysSelected}
            onClick={submit}
          >
            {t(editing ? "schedules.dialog.save" : "schedules.dialog.create")}
          </button>
        </>
      }
    >
      <label className="field">
        {t("schedules.dialog.nameLabel")}
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        {nameError !== null && <span className="field-error">{nameError}</span>}
      </label>

      <label className="field">
        {t("schedules.dialog.actionLabel")}
        {/* aria-label: a select wrapped in a label otherwise computes its
            accessible name from the label's text content INCLUDING its own
            option texts — an unusable name for AT and tests alike. */}
        <select
          value={action}
          disabled={editing}
          aria-label={t("schedules.dialog.actionLabel")}
          onChange={(e) => setAction(e.target.value as ScheduleAction)}
        >
          {(editing ? [action] : permittedActions).map((a) => (
            <option key={a} value={a}>
              {t(ACTION_LABEL[a])}
            </option>
          ))}
        </select>
      </label>
      {editing && <p className="sub">{t("schedules.dialog.actionLocked")}</p>}

      <div className="schedule-cadence">
        <label className="field">
          {t("schedules.dialog.cadenceLabel")}
          <select
            aria-label={t("schedules.dialog.cadenceLabel")}
            value={
              cadenceMode === "interval"
                ? "interval"
                : cadenceMode === "cron"
                  ? "cron"
                  : dwRepeat === "everyDay"
                    ? "daily"
                    : "weekly"
            }
            onChange={(e) => {
              const v = e.target.value;
              if (v === "interval") {
                setCadenceMode("interval");
              } else if (v === "daily") {
                setCadenceMode("dailyWeekly");
                setDwRepeat("everyDay");
              } else if (v === "weekly") {
                setCadenceMode("dailyWeekly");
                setDwRepeat("specificDays");
              } else {
                setCadenceMode("cron");
              }
            }}
          >
            <option value="interval">
              {t("schedules.dialog.cadence.interval")}
            </option>
            <option value="daily">{t("schedules.dialog.cadence.daily")}</option>
            <option value="weekly">
              {t("schedules.dialog.cadence.weekly")}
            </option>
            <option value="cron">{t("schedules.dialog.cadence.cron")}</option>
          </select>
        </label>

        {cadenceMode === "interval" && (
          <span className="schedule-interval-row">
            {t("schedules.dialog.intervalLabel")}
            <input
              type="number"
              min={1}
              aria-label={t("schedules.dialog.intervalLabel")}
              value={intervalValue}
              onChange={(e) => setIntervalValue(e.target.value)}
            />
            <select
              aria-label={t("schedules.dialog.intervalUnitLabel")}
              value={intervalUnit}
              onChange={(e) =>
                setIntervalUnit(e.target.value as "minutes" | "hours")
              }
            >
              <option value="minutes">
                {t("schedules.dialog.unit.minutes")}
              </option>
              <option value="hours">{t("schedules.dialog.unit.hours")}</option>
            </select>
          </span>
        )}

        {cadenceMode === "dailyWeekly" && dwRepeat === "specificDays" && (
          <>
            <div className="schedule-day-picker">
              {DAY_NUMBERS.map((d) => (
                <label key={d} className="checkbox">
                  <input
                    type="checkbox"
                    checked={dwDays.has(d)}
                    onChange={() => {
                      setDwDays((prev) => {
                        const next = new Set(prev);
                        if (next.has(d)) next.delete(d);
                        else next.add(d);
                        return next;
                      });
                    }}
                  />
                  {t(DAY_LABELS[d])}
                </label>
              ))}
            </div>
            {noDaysSelected && (
              <span className="field-error">
                {t("schedules.dialog.noDaysSelected")}
              </span>
            )}
          </>
        )}

        {cadenceMode === "dailyWeekly" && (
          <div className="schedule-time-row">
            <label>
              {t("schedules.dialog.hourLabel")}
              <input
                type="number"
                min={0}
                max={23}
                aria-label={t("schedules.dialog.hourLabel")}
                value={dwHour}
                onChange={(e) => setDwHour(e.target.value)}
              />
            </label>
            <span className="schedule-time-separator">:</span>
            <label>
              {t("schedules.dialog.minuteLabel")}
              <input
                type="number"
                min={0}
                max={59}
                aria-label={t("schedules.dialog.minuteLabel")}
                value={dwMinute}
                onChange={(e) => setDwMinute(e.target.value)}
              />
            </label>
          </div>
        )}

        {cadenceMode === "cron" && (
          <>
            <input
              type="text"
              aria-label={t("schedules.dialog.cronLabel")}
              placeholder={t("schedules.dialog.cronPlaceholder")}
              value={cron}
              onChange={(e) => setCron(e.target.value)}
            />
            <p className="schedule-cron-help">
              {t("schedules.dialog.cronHelp")}
            </p>
          </>
        )}

        {cadenceError !== null && (
          <span className="field-error">{cadenceError}</span>
        )}
      </div>

      <NextRunsPreview
        communityId={communityId}
        serverId={serverId}
        cadenceBody={JSON.stringify({
          ...buildCadence(),
          timezone,
        })}
        timezone={timezone}
        isInterval={cadenceMode === "interval"}
      />

      <label className="field">
        {t("schedules.dialog.timezoneLabel")}
        {/* aria-label: see the action select — zone options would otherwise
            pollute the computed accessible name. */}
        <select
          value={timezone}
          aria-label={t("schedules.dialog.timezoneLabel")}
          onChange={(e) => setTimezone(e.target.value)}
        >
          {tzOptions.map((zone) => (
            <option key={zone} value={zone}>
              {zone}
            </option>
          ))}
        </select>
        {timezoneError !== null && (
          <span className="field-error">{timezoneError}</span>
        )}
      </label>

      {action === "command" && (
        <label className="field">
          {t("schedules.dialog.commandLabel")}
          <input
            type="text"
            placeholder={t("schedules.dialog.commandPlaceholder")}
            value={command}
            onChange={(e) => setCommand(e.target.value)}
          />
          {commandError !== null && (
            <span className="field-error">{commandError}</span>
          )}
        </label>
      )}

      {showWarnings && (
        <fieldset className="field schedules-warnings">
          <legend>{t("schedules.dialog.warningsLabel")}</legend>
          <p className="sub">{t("schedules.dialog.warningsHint")}</p>
          {warnings.map((step, index) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional
            <div className="schedules-warning-row" key={index}>
              <input
                type="number"
                min={MIN_OFFSET_MINUTES}
                max={MAX_OFFSET_MINUTES}
                aria-label={t("schedules.dialog.warning.offset")}
                value={step.offset}
                onChange={(e) =>
                  updateWarning(index, { offset: e.target.value })
                }
              />
              <input
                type="text"
                aria-label={t("schedules.dialog.warning.message")}
                value={step.message}
                onChange={(e) =>
                  updateWarning(index, { message: e.target.value })
                }
              />
              <button
                type="button"
                className="btn sm"
                onClick={() => removeWarning(index)}
              >
                {t("schedules.dialog.warning.remove")}
              </button>
            </div>
          ))}
          {warnings.length < MAX_WARNING_STEPS && (
            <button type="button" className="btn sm" onClick={addWarning}>
              {t("schedules.dialog.warning.add")}
            </button>
          )}
          {warningError !== null && (
            <span className="field-error">{warningError}</span>
          )}
        </fieldset>
      )}

      <label className="checkbox">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
        />
        {t("schedules.dialog.enabledLabel")}
      </label>
    </Modal>
  );
}

/** Read-only run-history view: outcome + sanitized detail + timestamps (#1837). */
function RunHistoryDialog({
  communityId,
  serverId,
  schedule,
  onClose,
}: {
  communityId: string;
  serverId: string;
  schedule: ScheduleResponse;
  onClose: () => void;
}) {
  const query = useQuery({
    queryKey: runsKey(communityId, serverId, schedule.id),
    queryFn: ({ signal }) =>
      api.get(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/schedules/{schedule_id}/runs",
          {
            community_id: communityId,
            server_id: serverId,
            schedule_id: schedule.id,
          },
        ),
        { signal },
      ),
  });

  return (
    <Modal
      open={true}
      title={t("schedules.runs.title", { name: schedule.name })}
      onClose={onClose}
      footer={
        <button type="button" className="btn" onClick={onClose}>
          {t("common.close")}
        </button>
      }
    >
      <RunHistoryBody
        pending={query.isPending}
        runs={query.data}
        errored={query.isError}
      />
    </Modal>
  );
}

function RunHistoryBody({
  pending,
  runs,
  errored,
}: {
  pending: boolean;
  runs: ScheduleRunResponse[] | undefined;
  errored: boolean;
}) {
  if (pending) {
    return <p className="sub">{t("schedules.runs.loading")}</p>;
  }
  if (runs === undefined) {
    return (
      <p className="field-error">
        {errored ? t("schedules.runs.loadError") : t("schedules.runs.empty")}
      </p>
    );
  }
  if (runs.length === 0) {
    return <p className="sub">{t("schedules.runs.empty")}</p>;
  }
  return (
    <table className="data schedules-runs">
      <thead>
        <tr>
          <th>{t("schedules.runs.col.outcome")}</th>
          <th>{t("schedules.runs.col.detail")}</th>
          <th>{t("schedules.runs.col.started")}</th>
          <th>{t("schedules.runs.col.finished")}</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => {
          const label = OUTCOME_LABEL[run.outcome];
          return (
            <tr key={run.id}>
              <td>
                <span className={`badge run-${run.outcome}`}>
                  {label !== undefined ? t(label) : run.outcome}
                </span>
              </td>
              <td>{run.detail ?? t("schedules.none")}</td>
              <td>{formatDateTime(run.started_at)}</td>
              <td>{formatDateTime(run.finished_at)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** Split an interval in seconds into a form value + unit (hours if clean). */
function intervalToForm(seconds: number): {
  value: string;
  unit: "minutes" | "hours";
} {
  if (seconds % 3600 === 0) {
    return { value: String(seconds / 3600), unit: "hours" };
  }
  return { value: String(seconds / 60), unit: "minutes" };
}

/**
 * Debounced preview of the next 5 schedule occurrences (issue #1867).
 *
 * Fetches from the preview API endpoint on cadence/timezone changes with a
 * 500ms debounce. Shows validation errors inline (reuses the cadenceError
 * pattern). The cadenceDeps array is used as the useEffect dependency to
 * trigger re-fetches.
 */
/** Format an ISO datetime in the given IANA timezone (the schedule's zone). */
function formatInTimezone(iso: string, tz: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, { timeZone: tz });
  } catch {
    // Invalid zone (shouldn't happen, but fall back to browser-local).
    return new Date(iso).toLocaleString();
  }
}

function NextRunsPreview({
  communityId,
  serverId,
  cadenceBody,
  timezone,
  isInterval,
}: {
  communityId: string;
  serverId: string;
  /** Pre-serialized JSON body for the preview request. Changes trigger a
   *  debounced re-fetch. */
  cadenceBody: string;
  /** The schedule's IANA timezone, for formatting the preview datetimes. */
  timezone: string;
  /** Whether the cadence is interval (shows approximation note). */
  isInterval: boolean;
}) {
  const [runs, setRuns] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountRef = useRef(true);

  useEffect(() => {
    const controller = new AbortController();
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
    }

    const doFetch = () => {
      setLoading(true);
      setError(null);
      api
        .post(
          apiPath(
            "/api/communities/{community_id}/servers/{server_id}/schedules/preview",
            { community_id: communityId, server_id: serverId },
          ),
          {
            body: cadenceBody,
            signal: controller.signal,
          },
        )
        .then((data) => {
          setRuns((data as { next_runs: string[] }).next_runs);
          setLoading(false);
        })
        .catch((err: unknown) => {
          if (err instanceof DOMException && err.name === "AbortError") return;
          setLoading(false);
          if (err instanceof ApiError) {
            switch (err.reason) {
              case "invalid_cron":
                setError(t("schedules.error.invalidCron"));
                return;
              case "invalid_cadence":
                setError(t("schedules.error.invalidCadence"));
                return;
              case "invalid_timezone":
                setError(t("schedules.error.invalidTimezone"));
                return;
            }
          }
          setRuns(null);
        });
    };

    if (mountRef.current) {
      mountRef.current = false;
      doFetch();
    } else {
      timerRef.current = setTimeout(doFetch, 500);
    }

    return () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
      }
      controller.abort();
    };
  }, [communityId, serverId, cadenceBody]);

  return (
    <div className="schedule-preview" data-testid="next-runs-preview">
      <strong>{t("schedules.dialog.nextRuns")}</strong>
      {loading && (
        <p className="sub">{t("schedules.dialog.nextRunsLoading")}</p>
      )}
      {error !== null && <span className="field-error">{error}</span>}
      {runs !== null && !loading && error === null && (
        <>
          <ul className="schedule-preview-list">
            {runs.map((run) => (
              <li key={run}>{formatInTimezone(run, timezone)}</li>
            ))}
          </ul>
          {isInterval && (
            <p className="schedule-preview-approx">
              {t("schedules.dialog.nextRunsApproximate")}
            </p>
          )}
        </>
      )}
    </div>
  );
}
