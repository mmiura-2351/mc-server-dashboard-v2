import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { Modal } from "../components/Modal.tsx";
import { useToast } from "../components/Toast.tsx";
import { heartbeatAge, humanizeBytes, statusPill } from "../format.ts";
import { t } from "../i18n/index.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";

// Platform admin Workers page (WEBUI_SPEC.md 6.12): the full fleet table with a
// per-worker drain/undrain toggle. Drain is `PUT /workers/{id}/drain` (200 with
// a `DrainResponse` body) and undrain is `DELETE /workers/{id}/drain` (204, the
// worker:manage axis).
// The status pill and heartbeat-age helpers are shared with the Overview page
// rather than duplicated (#477).

type WorkerResponse = components["schemas"]["WorkerResponse"];

// A modest poll keeps the fleet view fresh without a WS: heartbeats and drain
// state change on the order of seconds, so 12s is a reasonable middle ground.
const REFRESH_INTERVAL_MS = 12_000;

export function AdminWorkersPage() {
  const workersQuery = useQuery({
    queryKey: ["workers"],
    queryFn: () => api.get("/api/workers"),
    refetchInterval: REFRESH_INTERVAL_MS,
  });

  return (
    <div className="admin-workers">
      <div className="page-head">
        <div>
          <h1>{t("page.adminWorkers")}</h1>
          <div className="sub">{t("admin.workers.subtitle")}</div>
        </div>
      </div>

      {workersQuery.isPending ? (
        <p className="sub" role="status">
          {t("admin.workers.loading")}
        </p>
      ) : workersQuery.isError ? (
        <p className="field-error" role="alert">
          {t("admin.workers.loadError")}
        </p>
      ) : (
        <Loaded workers={workersQuery.data?.workers ?? []} />
      )}
    </div>
  );
}

function Loaded({ workers }: { workers: WorkerResponse[] }) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  // The worker pending confirmation; `draining` distinguishes the action so the
  // one dialog serves both directions of the toggle.
  const [confirming, setConfirming] = useState<WorkerResponse | null>(null);

  const drain = useMutation({
    mutationFn: (worker: WorkerResponse) =>
      api.put(
        apiPath("/api/workers/{worker_id}/drain", { worker_id: worker.id }),
      ),
    onSuccess: () => {
      showToast(t("admin.workers.drained"), "success");
      queryClient.invalidateQueries({ queryKey: ["workers"] });
    },
    onError: (error) => {
      if (onForbidden(error)) {
        return;
      }
      showToast(t("admin.workers.drainError"), "error");
    },
    onSettled: () => setConfirming(null),
  });

  const undrain = useMutation({
    mutationFn: (worker: WorkerResponse) =>
      api.delete(
        apiPath("/api/workers/{worker_id}/drain", { worker_id: worker.id }),
      ),
    onSuccess: () => {
      showToast(t("admin.workers.undrained"), "success");
      queryClient.invalidateQueries({ queryKey: ["workers"] });
    },
    onError: (error) => {
      if (onForbidden(error)) {
        return;
      }
      showToast(t("admin.workers.undrainError"), "error");
    },
    onSettled: () => setConfirming(null),
  });

  const isDraining = confirming?.status === "draining";

  return (
    <>
      <div className="card">
        {workers.length === 0 ? (
          <p className="sub">{t("admin.workers.empty")}</p>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>{t("admin.workers.colWorker")}</th>
                <th>{t("admin.workers.colStatus")}</th>
                <th>{t("admin.workers.colVersion")}</th>
                <th>{t("admin.workers.colDrivers")}</th>
                <th>{t("admin.workers.colLoad")}</th>
                <th>{t("admin.workers.colResources")}</th>
                <th>{t("admin.workers.colHeartbeat")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {workers.map((w) => (
                <tr key={w.id}>
                  <td className="num">
                    <strong>{w.id}</strong>
                  </td>
                  <td>
                    <span className={`pill ${statusPill(w.status)}`}>
                      {w.status}
                    </span>
                  </td>
                  <td className="num">{w.version}</td>
                  <td>
                    {w.capabilities.drivers.map((d) => (
                      <span key={d} className="badge">
                        {d}
                      </span>
                    ))}
                  </td>
                  <td className="num">
                    {w.assigned_count} / {w.capabilities.max_servers}
                  </td>
                  <td className="num">
                    {w.capabilities.resources.cpu_cores}
                    {t("admin.workers.cpuCores")} ·{" "}
                    {humanizeBytes(w.capabilities.resources.memory_bytes)}
                  </td>
                  <td>{heartbeatAge(w.last_heartbeat_at)}</td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn sm"
                      onClick={() => setConfirming(w)}
                    >
                      {w.status === "draining"
                        ? t("admin.workers.undrain")
                        : t("admin.workers.drain")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="notice info">{t("admin.workers.notice")}</div>

      <Modal
        open={confirming !== null}
        title={
          isDraining
            ? t("admin.workers.undrainDialogTitle")
            : t("admin.workers.drainDialogTitle")
        }
        onClose={() => setConfirming(null)}
        footer={
          <>
            <button
              type="button"
              className="btn ghost"
              onClick={() => setConfirming(null)}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className={isDraining ? "btn primary" : "btn danger"}
              disabled={drain.isPending || undrain.isPending}
              onClick={() => {
                if (confirming === null) {
                  return;
                }
                if (isDraining) {
                  undrain.mutate(confirming);
                } else {
                  drain.mutate(confirming);
                }
              }}
            >
              {isDraining
                ? t("admin.workers.undrainConfirm")
                : t("admin.workers.drainConfirm")}
            </button>
          </>
        }
      >
        <p>
          {isDraining
            ? t("admin.workers.undrainDialogBody")
            : t("admin.workers.drainDialogBody")}
        </p>
      </Modal>
    </>
  );
}
