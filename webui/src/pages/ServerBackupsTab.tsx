/**
 * Server detail — Backups tab (WEBUI_SPEC.md 6.7).
 *
 * Stats header (count / total size / newest / oldest), a backups table with
 * per-row download / restore / delete, a create button (works while running —
 * the API takes the save-all + snapshot path), an upload picker, and the
 * per-server schedule field backed by the `backup_interval_hours` config key.
 *
 * Restore requires the server stopped (the API answers 409 `server_not_stopped`
 * otherwise). Rather than a fragile auto-chain, the restore dialog explains the
 * requirement, offers a one-click stop while the server is not stopped, and asks
 * the user to retry once it has stopped — the honest two-step (WEBUI_SPEC.md 6.7).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { ApiError, api } from "../api/client.ts";
import { downloadFile } from "../api/download.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { ConfirmDialog } from "../components/ConfirmDialog.tsx";
import { Modal } from "../components/Modal.tsx";
import { ResizableTable } from "../components/ResizableColumns.tsx";
import { useToast } from "../components/Toast.tsx";
import { humanizeBytes } from "../format.ts";
import { type TranslationKey, t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { serverKey } from "./serverKey.ts";
import { normalizeState } from "./serverState.ts";
import { serversKey } from "./useCommunityEvents.ts";

type ServerResponse = components["schemas"]["ServerResponse"];
type BackupResponse = components["schemas"]["BackupResponse"];

const BACKUP_INTERVAL_KEY = "backup_interval_hours";

/** Backups list query key — scoped to the server, invalidated on every change. */
function backupsKey(communityId: string, serverId: string) {
  return ["backups", communityId, serverId] as const;
}

/** Backups statistics query key. */
function statsKey(communityId: string, serverId: string) {
  return ["backups", communityId, serverId, "statistics"] as const;
}

// Map a create/upload error reason to a specific message; otherwise generic.
function createErrorMessage(error: unknown): TranslationKey {
  if (error instanceof ApiError) {
    switch (error.reason) {
      case "server_unsettled":
        return "backups.error.unsettled";
      case "invalid_archive":
        return "backups.error.invalidArchive";
      case "worker_unavailable":
        return "backups.error.workerUnavailable";
    }
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
  const serverId = server.id;
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [restoreTarget, setRestoreTarget] = useState<BackupResponse | null>(
    null,
  );
  const [deleteTarget, setDeleteTarget] = useState<BackupResponse | null>(null);

  const canRead = can("backup:read", { serverId });
  const canCreate = can("backup:create", { serverId });
  const canRestore = can("backup:restore", { serverId });
  const canDelete = can("backup:delete", { serverId });
  const canSchedule = can("backup:schedule", { serverId });
  // The schedule is saved through the shared server PATCH, which the API gates on
  // server:update. Without it a backup:schedule-only user can edit the field but
  // the Save would 403, so the field is shown read-only.
  const canUpdate = can("server:update", { serverId });
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
    queryFn: () =>
      api.get(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/backups/statistics",
          { community_id: communityId, server_id: serverId },
        ),
      ),
  });

  const listQuery = useQuery({
    queryKey: backupsKey(communityId, serverId),
    enabled: canRead,
    queryFn: () =>
      api.get(
        apiPath("/api/communities/{community_id}/servers/{server_id}/backups", {
          community_id: communityId,
          server_id: serverId,
        }),
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
      return api.postForm(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/backups/upload",
          { community_id: communityId, server_id: serverId },
        ),
        form,
      );
    },
    onSuccess: () => {
      showToast(t("backups.uploaded"), "success");
      refresh();
    },
    onError,
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
  if (listQuery.isError || statsQuery.isError) {
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
        />
        <Stat
          labelKey="backups.stat.newest"
          value={stats.newest ?? t("backups.none")}
        />
        <Stat
          labelKey="backups.stat.oldest"
          value={stats.oldest ?? t("backups.none")}
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
                  upload.mutate(file);
                }
                e.target.value = "";
              }}
            />
          </>
        )}
        {canSchedule && (
          <ScheduleField
            server={server}
            communityId={communityId}
            canEdit={canUpdate}
            onError={onError}
          />
        )}
      </div>

      <div className="card backups-table">
        <ResizableTable storageKey="mcsd.colw.backups" className="data">
          <thead>
            <tr>
              <th>{t("backups.col.created")}</th>
              <th>{t("backups.col.source")}</th>
              <th>{t("backups.col.size")}</th>
              <th>{t("backups.col.creator")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {backups.length === 0 ? (
              <tr>
                <td colSpan={5} className="sub">
                  {t("backups.empty")}
                </td>
              </tr>
            ) : (
              backups.map((backup) => (
                <tr key={backup.id}>
                  <td>{backup.created_at}</td>
                  <td>
                    <span className="badge">{backup.source}</span>
                  </td>
                  <td className="num">
                    {backup.size_bytes !== null
                      ? humanizeBytes(backup.size_bytes)
                      : t("backups.unknownSize")}
                  </td>
                  <td title={backup.created_by ?? undefined}>
                    {backup.created_by ?? t("backups.unknownCreator")}
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

function Stat({
  labelKey,
  value,
}: {
  labelKey: TranslationKey;
  value: string;
}) {
  return (
    <div className="metric">
      <div className="metric-label">{t(labelKey)}</div>
      <div className="metric-value">{value}</div>
    </div>
  );
}

// The schedule is one config key (`backup_interval_hours`) saved through the
// shared server PATCH, reusing the type-preserving rule: a value is sent as a
// NUMBER so the API's non-bool-int validation accepts it; a blank field omits
// the key (no schedule). The rest of the config blob round-trips untouched.
function ScheduleField({
  server,
  communityId,
  canEdit,
  onError,
}: {
  server: ServerResponse;
  communityId: string;
  canEdit: boolean;
  onError: (error: unknown) => void;
}) {
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const config = server.config as Record<string, unknown>;
  const current = config[BACKUP_INTERVAL_KEY];
  const [hours, setHours] = useState(
    typeof current === "number" ? String(current) : "",
  );

  const save = useMutation({
    mutationFn: () => {
      const next: Record<string, unknown> = { ...config };
      if (hours.trim() === "") {
        delete next[BACKUP_INTERVAL_KEY];
      } else {
        next[BACKUP_INTERVAL_KEY] = Number(hours);
      }
      return api.patch(
        apiPath("/api/communities/{community_id}/servers/{server_id}", {
          community_id: communityId,
          server_id: server.id,
        }),
        { body: JSON.stringify({ config: next }) },
      );
    },
    onSuccess: () => {
      showToast(t("backups.schedule.saved"), "success");
      queryClient.invalidateQueries({
        queryKey: serverKey(communityId, server.id),
      });
      queryClient.invalidateQueries({ queryKey: serversKey(communityId) });
    },
    onError: (error) => {
      if (
        error instanceof ApiError &&
        error.reason === "invalid_backup_schedule"
      ) {
        showToast(t("backups.error.invalidSchedule"), "error");
        return;
      }
      onError(error);
    },
  });

  return (
    <span className="backups-schedule">
      <span className="field-inline">
        {t("backups.schedule.label")}
        <input
          type="number"
          min={1}
          aria-label={t("backups.schedule.label")}
          value={hours}
          disabled={!canEdit}
          onChange={(e) => setHours(e.target.value)}
        />
        {t("backups.schedule.unit")}
      </span>
      {canEdit && (
        <button
          type="button"
          className="btn sm"
          disabled={save.isPending}
          onClick={() => save.mutate()}
        >
          {t("backups.schedule.save")}
        </button>
      )}
    </span>
  );
}

// Restore is stopped-only. While the server is not stopped this dialog explains
// the requirement and offers a one-click stop; the user retries restore once it
// has settled (the honest two-step — no auto-chain). When stopped it is a
// typed-confirm restore. A 409 server_not_stopped (state changed mid-flight) is
// surfaced specifically.
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
  const phrase = t("backups.restoreDialog.phrase");

  const invalidateServer = () => {
    queryClient.invalidateQueries({
      queryKey: serverKey(communityId, server.id),
    });
    queryClient.invalidateQueries({ queryKey: serversKey(communityId) });
  };

  const restore = useMutation({
    mutationFn: () =>
      api.post(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/backups/{backup_id}/restore",
          {
            community_id: communityId,
            server_id: server.id,
            backup_id: backup.id,
          },
        ),
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
      showToast(t("backups.error.generic"), "error");
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
      showToast(t("backups.error.generic"), "error");
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
              disabled={typed !== phrase || restore.isPending}
              onClick={() => restore.mutate()}
            >
              {t("backups.restoreDialog.confirm")}
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
