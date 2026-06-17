import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client.ts";
import type { components } from "../api/schema";
import { heartbeatAge, humanizeBytes, statusPill } from "../format.ts";
import { t } from "../i18n/index.ts";

// Platform admin Overview (WEBUI_SPEC.md 6.12): worker count by status, total
// servers running, global backup stats, jar-pool stats. All four read from the
// platform-admin `[A]` endpoints; the worker tallies are derived client-side
// from the fleet list (the API gives the list, not the counts) (#474).

type WorkerResponse = components["schemas"]["WorkerResponse"];

// Matches AdminWorkersPage's interval so heartbeat ages and load stay fresh
// without a separate polling policy (#791).
const REFRESH_INTERVAL_MS = 12_000;

export function AdminOverviewPage() {
  const workersQuery = useQuery({
    queryKey: ["workers"],
    queryFn: () => api.get("/api/workers"),
    refetchInterval: REFRESH_INTERVAL_MS,
  });
  const backupsQuery = useQuery({
    queryKey: ["backups", "statistics"],
    queryFn: () => api.get("/api/backups/statistics"),
    refetchInterval: REFRESH_INTERVAL_MS,
  });
  const jarPoolQuery = useQuery({
    queryKey: ["versions", "jar-pool", "stats"],
    queryFn: () => api.get("/api/versions/jar-pool/stats"),
    refetchInterval: REFRESH_INTERVAL_MS,
  });

  const isPending =
    workersQuery.isPending || backupsQuery.isPending || jarPoolQuery.isPending;
  const isError =
    workersQuery.isError || backupsQuery.isError || jarPoolQuery.isError;

  return (
    <div className="admin-overview">
      <div className="page-head">
        <div>
          <h1>{t("page.adminOverview")}</h1>
          <div className="sub">{t("admin.overview.subtitle")}</div>
        </div>
      </div>

      {isPending ? (
        <p className="sub" role="status">
          {t("admin.overview.loading")}
        </p>
      ) : isError ? (
        <p className="field-error" role="alert">
          {t("admin.overview.loadError")}
        </p>
      ) : (
        <Loaded
          workers={workersQuery.data?.workers ?? []}
          backups={backupsQuery.data}
          jarPool={jarPoolQuery.data}
        />
      )}
    </div>
  );
}

interface LoadedProps {
  workers: WorkerResponse[];
  backups: components["schemas"]["BackupStatisticsResponse"] | undefined;
  jarPool: components["schemas"]["JarPoolStatsResponse"] | undefined;
}

function Loaded({ workers, backups, jarPool }: LoadedProps) {
  const online = workers.filter((w) => w.status === "online").length;
  const draining = workers.filter((w) => w.status === "draining").length;
  const offline = workers.length - online - draining;
  const serversRunning = workers.reduce((sum, w) => sum + w.assigned_count, 0);

  return (
    <>
      <div className="grid cols-4 metric-tiles">
        <div className="card metric-tile">
          <div className="label">{t("admin.overview.workers")}</div>
          <div className="value">
            {online}
            <span className="unit">
              {" "}
              / {workers.length} {t("admin.overview.workersOnline")}
            </span>
          </div>
          <div className="hint">
            {draining} {t("admin.overview.workersDraining")} · {offline}{" "}
            {t("admin.overview.workersOffline")}
          </div>
        </div>
        <div className="card metric-tile">
          <div className="label">{t("admin.overview.servers")}</div>
          <div className="value">{serversRunning}</div>
          <div className="hint">{t("admin.overview.serversHint")}</div>
        </div>
        <div className="card metric-tile">
          <div className="label">{t("admin.overview.backups")}</div>
          <div className="value">
            {backups?.count ?? 0}
            <span className="unit">
              {" "}
              · {humanizeBytes(backups?.total_bytes ?? 0)}
            </span>
          </div>
          <div className="hint">
            {t("admin.overview.jarPool")}: {jarPool?.count ?? 0}{" "}
            {t("admin.overview.jars")} ·{" "}
            {humanizeBytes(jarPool?.total_bytes ?? 0)}
          </div>
        </div>
      </div>

      <div className="card">
        <h2>{t("admin.overview.fleet")}</h2>
        {workers.length === 0 ? (
          <p className="sub">{t("admin.overview.fleetEmpty")}</p>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>{t("admin.overview.fleetWorker")}</th>
                <th>{t("admin.overview.fleetStatus")}</th>
                <th>{t("admin.overview.fleetLoad")}</th>
                <th>{t("admin.overview.fleetHeartbeat")}</th>
              </tr>
            </thead>
            <tbody>
              {workers.map((w) => (
                <tr key={w.id}>
                  <td className="num">{w.id}</td>
                  <td>
                    <span className={`pill ${statusPill(w.status)}`}>
                      {w.status}
                    </span>
                  </td>
                  <td className="num">
                    {w.assigned_count} / {w.capabilities.max_servers}
                  </td>
                  <td>{heartbeatAge(w.last_heartbeat_at, t)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
