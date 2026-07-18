/**
 * Server detail — Backups tab (WEBUI_SPEC.md 6.7).
 *
 * Stats header (count / total size / newest / oldest), a backups table with
 * per-row download / restore / delete, a create button (works while running —
 * the API takes the save-all + snapshot path), and an upload picker. Scheduled
 * backups are no longer configured here: the FR-BAK-3 cadence moved to the
 * general scheduler (a first-class `backup` schedule, #1840), so the tab only
 * points `backup:schedule` holders at the Schedules surface. Those same holders
 * also get the scheduled-backup retention editor (keep-N / tiered / clear, the
 * #1841 API), which prunes only `scheduled` backups (#1843).
 *
 * Restore requires the server stopped (the API answers 409 `server_not_stopped`
 * otherwise). Rather than a fragile auto-chain, the restore dialog explains the
 * requirement, offers a one-click stop while the server is not stopped, and asks
 * the user to retry once it has stopped — the honest two-step (WEBUI_SPEC.md 6.7).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import {
  ApiError,
  api,
  isUploadAbortError,
  postFormWithProgress,
} from "../api/client.ts";
import { downloadFile } from "../api/download.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { ConfirmDialog } from "../components/ConfirmDialog.tsx";
import { Modal } from "../components/Modal.tsx";
import { ResizableTable } from "../components/ResizableColumns.tsx";
import { useToast } from "../components/Toast.tsx";
import { UploadProgress } from "../components/UploadProgress.tsx";
import { useUploadProgress } from "../components/useUploadProgress.ts";
import { formatDateTime, humanizeBytes, shortId } from "../format.ts";
import { type TranslationKey, t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { serverKey } from "./serverKey.ts";
import { normalizeState } from "./serverState.ts";
import { serversKey } from "./useCommunityEvents.ts";

type ServerResponse = components["schemas"]["ServerResponse"];
type BackupResponse = components["schemas"]["BackupResponse"];
type RetentionPolicyBody = components["schemas"]["RetentionPolicyBody"];

/** Backups list query key — scoped to the server, invalidated on every change. */
function backupsKey(communityId: string, serverId: string) {
  return ["backups", communityId, serverId] as const;
}

/** Backups statistics query key. */
function statsKey(communityId: string, serverId: string) {
  return ["backups", communityId, serverId, "statistics"] as const;
}

// Map a create/upload/restore error to a specific message; otherwise generic.
function createErrorMessage(error: unknown): TranslationKey {
  if (!(error instanceof ApiError)) return "backups.error.generic";

  // Check reason first (most specific).
  switch (error.reason) {
    case "server_unsettled":
      return "backups.error.unsettled";
    case "server_not_stopped":
      return "backups.error.serverMustBeStopped";
    case "server_busy":
      return "backups.error.serverBusy";
    case "invalid_archive":
      return "backups.error.invalidArchive";
    case "worker_unavailable":
      return "backups.error.workerUnavailable";
  }

  // Check status (less specific).
  switch (error.status) {
    case 413:
      return "backups.error.tooLarge";
    case 503:
      return "backups.error.workerUnavailable";
  }

  return "backups.error.generic";
}

export function ServerBackupsTab({
  server,
  communityId,
  can,
}: {
  server: ServerResponse;
  communityId: string;
  can: Can;
}) {
  const MAX_UPLOAD_BYTES = 512 * 1024 * 1024;
  const serverId = server.id;
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [restoreTarget, setRestoreTarget] = useState<BackupResponse | null>(
    null,
  );
  const [deleteTarget, setDeleteTarget] = useState<BackupResponse | null>(null);
  const progress = useUploadProgress();

  const canRead = can("backup:read", { serverId });
  const canCreate = can("backup:create", { serverId });
  const canRestore = can("backup:restore", { serverId });
  const canDelete = can("backup:delete", { serverId });
  // The FR-BAK-3 inline cadence field is retired (#1840): scheduled backups are
  // now a first-class `backup` schedule managed on the Schedules surface. Those
  // who used to hold `backup:schedule` are pointed there instead of editing a
  // config key here.
  const canSchedule = can("backup:schedule", { serverId });
  // The restore dialog's one-click stop hits the lifecycle stop endpoint, gated
  // on server:stop. Without it the user must ask an operator to stop the server.
  const canStop = can("server:stop", { serverId });

  const stopped = normalizeState(server.observed_state) === "stopped";

  const onError = (error: unknown) => {
    if (onForbidden(error)) {
      return;
    }
    showToast(t(createErrorMessage(error)), "error");
  };

  const statsQuery = useQuery({
    queryKey: statsKey(communityId, serverId),
    enabled: canRead,
    queryFn: ({ signal }) =>
      api.get(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/backups/statistics",
          { community_id: communityId, server_id: serverId },
        ),
        { signal },
      ),
  });

  const listQuery = useQuery({
    queryKey: backupsKey(communityId, serverId),
    enabled: canRead,
    queryFn: ({ signal }) =>
      api.get(
        apiPath("/api/communities/{community_id}/servers/{server_id}/backups", {
          community_id: communityId,
          server_id: serverId,
        }),
        { signal },
      ),
  });

  const refresh = () => {
    queryClient.invalidateQueries({
      queryKey: backupsKey(communityId, serverId),
    });
    queryClient.invalidateQueries({
      queryKey: statsKey(communityId, serverId),
    });
  };

  const create = useMutation({
    mutationFn: () =>
      api.post(
        apiPath("/api/communities/{community_id}/servers/{server_id}/backups", {
          community_id: communityId,
          server_id: serverId,
        }),
      ),
    onSuccess: () => {
      showToast(t("backups.created"), "success");
      refresh();
    },
    onError,
  });

  const upload = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      const signal = progress.start(file.size);
      return postFormWithProgress(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/backups/upload",
          { community_id: communityId, server_id: serverId },
        ),
        form,
        progress.onProgress,
        signal,
      );
    },
    onSuccess: () => {
      progress.reset();
      showToast(t("backups.uploaded"), "success");
      refresh();
    },
    onError: (error) => {
      progress.reset();
      if (isUploadAbortError(error)) return;
      onError(error);
    },
  });

  const download = useMutation({
    mutationFn: (backup: BackupResponse) =>
      downloadFile(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/backups/{backup_id}/download",
          {
            community_id: communityId,
            server_id: serverId,
            backup_id: backup.id,
          },
        ),
        `${backup.id}.tar.gz`,
      ),
    onError,
  });

  const remove = useMutation({
    mutationFn: (backup: BackupResponse) =>
      api.delete(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/backups/{backup_id}",
          {
            community_id: communityId,
            server_id: serverId,
            backup_id: backup.id,
          },
        ),
      ),
    onSuccess: () => {
      showToast(t("backups.deleted"), "success");
      refresh();
    },
    onError,
  });

  if (!canRead) {
    return <p className="sub">{t("backups.noRead")}</p>;
  }
  if (listQuery.isPending || statsQuery.isPending) {
    return <p className="sub">{t("backups.loading")}</p>;
  }
  // Error only when there is nothing to show (an initial load failed). A
  // failed background refetch retains `data`, so the cached page keeps
  // rendering through transient API blips (#1805).
  if (listQuery.data === undefined || statsQuery.data === undefined) {
    return <p className="field-error">{t("backups.loadError")}</p>;
  }

  const stats = statsQuery.data;
  const backups = listQuery.data.backups;
  const busy = create.isPending || upload.isPending;

  return (
    <section className="backups">
      <div className="card metrics-strip backups-stats">
        <Stat labelKey="backups.stat.count" value={String(stats.count)} />
        <Stat
          labelKey="backups.stat.totalSize"
          value={humanizeBytes(stats.total_bytes)}
          // total_bytes sums only backups with a recorded size; legacy NULL-size
          // rows are excluded (#281). Flag the figure as partial so it is not
          // misread as full usage (#640).
          hint={
            stats.unknown_size_count > 0
              ? t("backups.stat.totalSizePartial")
              : undefined
          }
        />
        <Stat
          labelKey="backups.stat.newest"
          value={
            stats.newest !== null
              ? formatDateTime(stats.newest)
              : t("backups.none")
          }
        />
        <Stat
          labelKey="backups.stat.oldest"
          value={
            stats.oldest !== null
              ? formatDateTime(stats.oldest)
              : t("backups.none")
          }
        />
      </div>

      <div className="backups-toolbar">
        {canCreate && (
          <>
            <button
              type="button"
              className="btn primary"
              disabled={busy}
              onClick={() => create.mutate()}
            >
              {t("backups.create")}
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => fileInput.current?.click()}
            >
              {t("backups.upload")}
            </button>
            <input
              ref={fileInput}
              type="file"
              hidden
              aria-label={t("backups.upload")}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file !== undefined) {
                  if (file.size > MAX_UPLOAD_BYTES) {
                    showToast(t("backups.error.tooLarge"), "error");
                  } else {
                    upload.mutate(file);
                  }
                }
                e.target.value = "";
              }}
            />
          </>
        )}
      </div>

      {canSchedule && (
        <div className="card backups-retention">
          <p className="sub backups-schedule-note">
            {t("backups.schedule.movedNote")}
          </p>
          <RetentionEditor
            key={retentionKey(server.backup_retention)}
            server={server}
            communityId={communityId}
            onError={onError}
          />
        </div>
      )}

      {progress.active && (
        <UploadProgress
          loaded={progress.loaded}
          total={progress.total}
          percent={progress.percent}
          elapsedMs={progress.elapsedMs}
          onCancel={progress.cancel}
        />
      )}

      <div className="card backups-table">
        <ResizableTable storageKey="mcsd.colw.backups" className="data">
          <thead>
            <tr>
              <th>{t("backups.col.created")}</th>
              <th>{t("backups.col.source")}</th>
              <th>{t("backups.col.condition")}</th>
              <th>{t("backups.col.size")}</th>
              <th>{t("backups.col.creator")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {backups.length === 0 ? (
              <tr>
                <td colSpan={6} className="sub">
                  {t("backups.empty")}
                </td>
              </tr>
            ) : (
              backups.map((backup) => (
                <tr key={backup.id}>
                  <td>{formatDateTime(backup.created_at)}</td>
                  <td>
                    <span className="badge">{backup.source}</span>
                  </td>
                  <td>
                    <HealthBadge health={backup.health} />
                  </td>
                  <td className="num">
                    {backup.size_bytes !== null
                      ? humanizeBytes(backup.size_bytes)
                      : t("backups.unknownSize")}
                  </td>
                  <td title={backup.created_by ?? undefined}>
                    {backup.created_by_username ??
                      (backup.created_by !== null
                        ? shortId(backup.created_by)
                        : t("backups.unknownCreator"))}
                  </td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn sm"
                      onClick={() => download.mutate(backup)}
                    >
                      {t("backups.download")}
                    </button>
                    {canRestore && (
                      <button
                        type="button"
                        className="btn sm"
                        onClick={() => setRestoreTarget(backup)}
                      >
                        {t("backups.restore")}
                      </button>
                    )}
                    {canDelete && (
                      <button
                        type="button"
                        className="btn sm danger"
                        onClick={() => setDeleteTarget(backup)}
                      >
                        {t("backups.delete")}
                      </button>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </ResizableTable>
      </div>

      {restoreTarget !== null && (
        <RestoreDialog
          backup={restoreTarget}
          server={server}
          communityId={communityId}
          stopped={stopped}
          canStop={canStop}
          onDone={refresh}
          onClose={() => setRestoreTarget(null)}
        />
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title={t("backups.deleteDialog.title")}
        body={t("backups.deleteDialog.body")}
        confirmPhrase={t("backups.deleteDialog.phrase")}
        confirmLabel={t("backups.deleteDialog.confirm")}
        promptLabel={t("backups.deleteDialog.prompt")}
        onConfirm={() => {
          const target = deleteTarget;
          setDeleteTarget(null);
          if (target !== null) {
            remove.mutate(target);
          }
        }}
        onClose={() => setDeleteTarget(null)}
      />
    </section>
  );
}

// Condition badge driven by the API `health` field (#745). A healthy backup
// renders nothing — only the at-risk states (quarantined / unknown, and any
// future value) earn a badge, so a clean list stays quiet. Each badge carries a
// plain-language hover title; no internal jargon leaks.
function HealthBadge({ health }: { health: string }) {
  if (health === "healthy") {
    return null;
  }
  const quarantined = health === "quarantined";
  return (
    <span
      className={`badge ${quarantined ? "health-quarantined" : "health-unknown"}`}
      title={t(
        quarantined
          ? "backups.health.quarantinedTitle"
          : "backups.health.unknownTitle",
      )}
    >
      {t(quarantined ? "backups.health.quarantined" : "backups.health.unknown")}
    </span>
  );
}

function Stat({
  labelKey,
  value,
  hint,
}: {
  labelKey: TranslationKey;
  value: string;
  hint?: string;
}) {
  return (
    <div className="metric">
      <div className="metric-label">{t(labelKey)}</div>
      <div className="metric-value">
        {value}
        {hint !== undefined && <span className="metric-unit"> ({hint})</span>}
      </div>
    </div>
  );
}

type RetentionMode = "none" | "keepLast" | "tiered";

/** Remount key: give the editor fresh state whenever the persisted policy changes. */
function retentionKey(policy: ServerResponse["backup_retention"]): string {
  return JSON.stringify(policy ?? null);
}

/** Read the persisted (loosely typed) policy into an editor mode + field strings. */
function readRetention(policy: ServerResponse["backup_retention"]): {
  mode: RetentionMode;
  keepLast: string;
  daily: string;
  weekly: string;
  monthly: string;
} {
  const p = (policy ?? {}) as RetentionPolicyBody;
  const str = (v: number | null | undefined) =>
    typeof v === "number" ? String(v) : "";
  if (typeof p.keep_last === "number") {
    return {
      mode: "keepLast",
      keepLast: String(p.keep_last),
      daily: "",
      weekly: "",
      monthly: "",
    };
  }
  if (
    typeof p.daily === "number" ||
    typeof p.weekly === "number" ||
    typeof p.monthly === "number"
  ) {
    return {
      mode: "tiered",
      keepLast: "",
      daily: str(p.daily),
      weekly: str(p.weekly),
      monthly: str(p.monthly),
    };
  }
  return { mode: "none", keepLast: "", daily: "", weekly: "", monthly: "" };
}

/** Parse a tier field: blank counts as 0; a non-integer or negative is invalid. */
function tierValue(raw: string): number | null {
  if (raw.trim() === "") return 0;
  const n = Number(raw);
  return Number.isInteger(n) && n >= 0 ? n : null;
}

// The scheduled-backup retention policy (#1841 API / #1843 UI), gated on
// backup:schedule. Three shapes: none (Save clears via DELETE), keep-N (PUT
// {keep_last}), and tiered (PUT {daily, weekly, monthly}). It prunes ONLY
// `scheduled` backups. The editor is keyed on the persisted policy so a save's
// refetch remounts it with the stored value; nothing mutates on mount. The
// client-side check mirrors the API's keep_last >= 1 XOR (each tier >= 0, at
// least one > 0) rule, with the server's invalid_retention_policy as a backstop.
function RetentionEditor({
  server,
  communityId,
  onError,
}: {
  server: ServerResponse;
  communityId: string;
  onError: (error: unknown) => void;
}) {
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const initial = readRetention(server.backup_retention);
  const hasPolicy = server.backup_retention != null;
  const [mode, setMode] = useState<RetentionMode>(initial.mode);
  const [keepLast, setKeepLast] = useState(initial.keepLast);
  const [daily, setDaily] = useState(initial.daily);
  const [weekly, setWeekly] = useState(initial.weekly);
  const [monthly, setMonthly] = useState(initial.monthly);
  const [error, setError] = useState<string | null>(null);

  const retentionPath = apiPath(
    "/api/communities/{community_id}/servers/{server_id}/backups/retention",
    { community_id: communityId, server_id: server.id },
  );

  const invalidate = () => {
    queryClient.invalidateQueries({
      queryKey: serverKey(communityId, server.id),
    });
    queryClient.invalidateQueries({ queryKey: serversKey(communityId) });
  };

  const onMutationError = (err: unknown) => {
    if (err instanceof ApiError && err.reason === "invalid_retention_policy") {
      setError(t("backups.retention.error.invalid"));
      return;
    }
    onError(err);
  };

  const save = useMutation({
    mutationFn: (body: RetentionPolicyBody) =>
      api.put(retentionPath, { body: JSON.stringify(body) }),
    onSuccess: () => {
      showToast(t("backups.retention.saved"), "success");
      invalidate();
      // A pruning PUT deletes scheduled-backup rows synchronously before
      // responding, so the backups table and stats strip must refetch too —
      // otherwise stale deleted rows remain visible until a manual refresh.
      queryClient.invalidateQueries({
        queryKey: backupsKey(communityId, server.id),
      });
      queryClient.invalidateQueries({
        queryKey: statsKey(communityId, server.id),
      });
    },
    onError: onMutationError,
  });

  const clear = useMutation({
    mutationFn: () => api.delete(retentionPath),
    onSuccess: () => {
      showToast(t("backups.retention.cleared"), "success");
      invalidate();
    },
    onError: onMutationError,
  });

  const submit = () => {
    setError(null);
    if (mode === "none") {
      clear.mutate();
      return;
    }
    if (mode === "keepLast") {
      const n = Number(keepLast);
      if (keepLast.trim() === "" || !Number.isInteger(n) || n < 1) {
        setError(t("backups.retention.error.keepLast"));
        return;
      }
      save.mutate({ keep_last: n });
      return;
    }
    const d = tierValue(daily);
    const w = tierValue(weekly);
    const m = tierValue(monthly);
    if (d === null || w === null || m === null || d + w + m === 0) {
      setError(t("backups.retention.error.tiered"));
      return;
    }
    save.mutate({ daily: d, weekly: w, monthly: m });
  };

  const busy = save.isPending || clear.isPending;
  // "none" with no stored policy is a no-op — skip the pointless DELETE.
  const disabled = busy || (mode === "none" && !hasPolicy);

  return (
    <div className="backups-retention-editor">
      <span className="field-inline">
        {t("backups.retention.modeLabel")}
        {/* aria-label: a select wrapped by its label text would fold the option
            texts into the accessible name — set it explicitly instead. */}
        <select
          aria-label={t("backups.retention.modeLabel")}
          value={mode}
          onChange={(e) => {
            setMode(e.target.value as RetentionMode);
            setError(null);
          }}
        >
          <option value="none">{t("backups.retention.mode.none")}</option>
          <option value="keepLast">
            {t("backups.retention.mode.keepLast")}
          </option>
          <option value="tiered">{t("backups.retention.mode.tiered")}</option>
        </select>
      </span>

      {mode === "keepLast" && (
        <span className="field-inline">
          {t("backups.retention.keepLastLabel")}
          <input
            type="number"
            min={1}
            aria-label={t("backups.retention.keepLastLabel")}
            value={keepLast}
            onChange={(e) => setKeepLast(e.target.value)}
          />
        </span>
      )}

      {mode === "tiered" && (
        <span className="backups-retention-tiers">
          <span className="field-inline">
            {t("backups.retention.dailyLabel")}
            <input
              type="number"
              min={0}
              aria-label={t("backups.retention.dailyLabel")}
              value={daily}
              onChange={(e) => setDaily(e.target.value)}
            />
          </span>
          <span className="field-inline">
            {t("backups.retention.weeklyLabel")}
            <input
              type="number"
              min={0}
              aria-label={t("backups.retention.weeklyLabel")}
              value={weekly}
              onChange={(e) => setWeekly(e.target.value)}
            />
          </span>
          <span className="field-inline">
            {t("backups.retention.monthlyLabel")}
            <input
              type="number"
              min={0}
              aria-label={t("backups.retention.monthlyLabel")}
              value={monthly}
              onChange={(e) => setMonthly(e.target.value)}
            />
          </span>
        </span>
      )}

      <button
        type="button"
        className="btn sm"
        disabled={disabled}
        onClick={submit}
      >
        {t("backups.retention.save")}
      </button>

      <p className="sub backups-retention-hint">
        {t(
          mode === "tiered"
            ? "backups.retention.tieredHint"
            : "backups.retention.hint",
        )}
      </p>
      {error !== null && (
        <span className="field-error backups-retention-error">{error}</span>
      )}
    </div>
  );
}

// Restore is stopped-only. While the server is not stopped this dialog explains
// the requirement and offers a one-click stop; the user retries restore once it
// has settled (the honest two-step — no auto-chain). When stopped it is a
// typed-confirm restore. A 409 server_not_stopped (state changed mid-flight) is
// surfaced specifically.
//
// A quarantined backup (health === "quarantined") is known-damaged: the restore
// is gated behind an extra explicit acknowledgement and sent with force=true,
// the operator override the API requires for a corrupt backup (#745). A healthy
// (or unknown) backup keeps the existing typed-confirm path with no force.
function RestoreDialog({
  backup,
  server,
  communityId,
  stopped,
  canStop,
  onDone,
  onClose,
}: {
  backup: BackupResponse;
  server: ServerResponse;
  communityId: string;
  stopped: boolean;
  canStop: boolean;
  onDone: () => void;
  onClose: () => void;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const [typed, setTyped] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const phrase = t("backups.restoreDialog.phrase");
  const quarantined = backup.health === "quarantined";

  const invalidateServer = () => {
    queryClient.invalidateQueries({
      queryKey: serverKey(communityId, server.id),
    });
    queryClient.invalidateQueries({ queryKey: serversKey(communityId) });
  };

  const restorePath = apiPath(
    "/api/communities/{community_id}/servers/{server_id}/backups/{backup_id}/restore",
    {
      community_id: communityId,
      server_id: server.id,
      backup_id: backup.id,
    },
  );

  const restore = useMutation({
    mutationFn: () =>
      // force=true is the operator override the API requires to restore a
      // known-corrupt backup (#745); a healthy backup restores without it. The
      // query suffix keeps the schema-literal path type via the cast.
      api.post(
        quarantined
          ? (`${restorePath}?force=true` as typeof restorePath)
          : restorePath,
      ),
    onSuccess: () => {
      showToast(t("backups.restored"), "success");
      onDone();
      onClose();
    },
    onError: (error) => {
      if (onForbidden(error)) {
        onClose();
        return;
      }
      if (error instanceof ApiError && error.reason === "server_not_stopped") {
        showToast(t("backups.error.notStopped"), "error");
        return;
      }
      showToast(t(createErrorMessage(error)), "error");
    },
  });

  const stop = useMutation({
    mutationFn: () =>
      api.post(
        apiPath("/api/communities/{community_id}/servers/{server_id}/stop", {
          community_id: communityId,
          server_id: server.id,
        }),
      ),
    onSuccess: () => {
      showToast(t("backups.restoreDialog.stopping"), "success");
      invalidateServer();
    },
    onError: (error) => {
      if (onForbidden(error)) {
        return;
      }
      showToast(t(createErrorMessage(error)), "error");
    },
  });

  return (
    <Modal
      open={true}
      title={t("backups.restoreDialog.title")}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={onClose}>
            {t("common.cancel")}
          </button>
          {stopped ? (
            <button
              type="button"
              className="btn danger"
              disabled={
                typed !== phrase ||
                (quarantined && !acknowledged) ||
                restore.isPending
              }
              onClick={() => restore.mutate()}
            >
              {t(
                quarantined
                  ? "backups.restoreDialog.damagedConfirm"
                  : "backups.restoreDialog.confirm",
              )}
            </button>
          ) : (
            canStop && (
              <button
                type="button"
                className="btn"
                disabled={stop.isPending}
                onClick={() => stop.mutate()}
              >
                {t("backups.restoreDialog.stop")}
              </button>
            )
          )}
        </>
      }
    >
      {stopped ? (
        <>
          {quarantined && (
            <p className="restore-damaged-warning">
              {t("backups.restoreDialog.damagedWarning")}
            </p>
          )}
          <p>{t("backups.restoreDialog.body")}</p>
          <label className="field">
            {t("backups.restoreDialog.prompt")}
            <input
              type="text"
              value={typed}
              placeholder={phrase}
              onChange={(e) => setTyped(e.target.value)}
            />
          </label>
          {quarantined && (
            <label className="restore-ack-row">
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(e) => setAcknowledged(e.target.checked)}
              />
              {t("backups.restoreDialog.damagedAck")}
            </label>
          )}
        </>
      ) : (
        <>
          <p>{t("backups.restoreDialog.blocked")}</p>
          <p className="sub">
            {t(
              canStop
                ? "backups.restoreDialog.blockedHint"
                : "backups.restoreDialog.blockedNoStop",
            )}
          </p>
        </>
      )}
    </Modal>
  );
}
