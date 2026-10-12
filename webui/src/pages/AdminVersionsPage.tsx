import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import { SimpleConfirmDialog } from "../components/SimpleConfirmDialog.tsx";
import { useToast } from "../components/Toast.tsx";
import { humanizeBytes } from "../format.ts";
import { t } from "../i18n/index.ts";
import { classifyQueryResult } from "../queryState.ts";

// The catalog provides ordered versions but no freshness timestamp; show counts and latest versions only.

export function AdminVersionsPage() {
  const typesQuery = useQuery({
    queryKey: ["versions", "types"],
    queryFn: ({ signal }) => api.get("/api/versions", { signal }),
  });
  const typesState = classifyQueryResult(typesQuery);

  return (
    <div className="admin-versions">
      <div className="page-head">
        <div>
          <h1>{t("page.adminVersions")}</h1>
          <div className="sub">{t("admin.versions.subtitle")}</div>
        </div>
      </div>

      {typesState.kind === "pending" ? (
        <p className="sub" role="status">
          {t("admin.versions.loading")}
        </p>
      ) : /* Retain cached content when only a background refetch fails. */
      typesState.kind === "error" ? (
        <p className="field-error" role="alert">
          {t("admin.versions.loadError")}
        </p>
      ) : (
        <Catalog types={typesState.data.server_types} />
      )}

      <JarPool />
    </div>
  );
}

function Catalog({ types }: { types: string[] }) {
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  // Per-type queries are independent of the type list; refresh must invalidate both.
  const versionQueries = useQueries({
    queries: types.map((type) => ({
      queryKey: ["versions", type],
      queryFn: ({ signal }) =>
        api.get(apiPath("/api/versions/{server_type}", { server_type: type }), {
          signal,
        }),
    })),
  });

  const refresh = useMutation({
    mutationFn: (serverType: string | null) =>
      api.post(
        (serverType === null
          ? "/api/versions/refresh"
          : `/api/versions/refresh?server_type=${encodeURIComponent(serverType)}`) as never,
      ),
    onSuccess: (_data, serverType) => {
      // Invalidate catalog keys only; JAR-pool statistics are refreshed by garbage collection.
      void queryClient.invalidateQueries({
        predicate: (query) =>
          query.queryKey[0] === "versions" && query.queryKey[1] !== "jar-pool",
      });
      showToast(
        serverType === null
          ? t("admin.versions.refreshedAll")
          : t("admin.versions.refreshedOne", { type: serverType }),
        "success",
      );
    },
    onError: () => {
      showToast(t("admin.versions.refreshError"), "error");
    },
  });

  const busyTarget =
    refresh.isPending && refresh.variables !== undefined
      ? (refresh.variables ?? "all")
      : null;

  return (
    <>
      <div className="page-head">
        <h2>{t("admin.versions.catalog")}</h2>
        <div className="actions">
          <button
            type="button"
            className="btn"
            disabled={refresh.isPending}
            onClick={() => refresh.mutate(null)}
          >
            {busyTarget === "all"
              ? t("admin.versions.refreshing")
              : t("admin.versions.refreshAll")}
          </button>
        </div>
      </div>

      <div className="card table-card">
        {types.length === 0 ? (
          <p className="sub">{t("admin.versions.empty")}</p>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>{t("admin.versions.type")}</th>
                <th>{t("admin.versions.count")}</th>
                <th>{t("admin.versions.latest")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {types.map((type, i) => {
                const q = versionQueries[i];
                const versions = q?.data?.versions ?? [];
                return (
                  <tr key={type}>
                    <td>
                      <span className="badge type">{type}</span>
                    </td>
                    <td className="num">
                      {q?.isError ? "—" : q?.isPending ? "…" : versions.length}
                    </td>
                    <td className="num">
                      {q?.isError
                        ? t("admin.versions.typeError")
                        : q?.isPending
                          ? "…"
                          : (versions[0] ?? "—")}
                    </td>
                    <td className="row-actions">
                      <button
                        type="button"
                        className="btn sm"
                        disabled={refresh.isPending}
                        onClick={() => refresh.mutate(type)}
                      >
                        {busyTarget === type
                          ? t("admin.versions.refreshing")
                          : t("admin.versions.refresh")}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function JarPool() {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const statsQuery = useQuery({
    queryKey: ["versions", "jar-pool", "stats"],
    queryFn: ({ signal }) =>
      api.get("/api/versions/jar-pool/stats", { signal }),
  });
  const statsState = classifyQueryResult(statsQuery);

  const gc = useMutation({
    mutationFn: () => api.post("/api/versions/jar-pool/gc"),
    onSuccess: (data) => {
      void queryClient.invalidateQueries({
        queryKey: ["versions", "jar-pool", "stats"],
      });
      showToast(
        t("admin.versions.gcDone", {
          bytes: humanizeBytes(data?.freed_bytes ?? 0),
          count: data?.deleted ?? 0,
        }),
        "success",
      );
    },
    onError: () => {
      showToast(t("admin.versions.gcError"), "error");
    },
  });

  return (
    <div className="card jar-pool">
      <h2>{t("admin.versions.jarPool")}</h2>
      {statsState.kind === "pending" ? (
        <p className="sub" role="status">
          {t("admin.versions.loading")}
        </p>
      ) : /* Retain cached content when only a background refetch fails. */
      statsState.kind === "error" ? (
        <p className="field-error" role="alert">
          {t("admin.versions.loadError")}
        </p>
      ) : (
        <dl className="kv">
          <dt>{t("admin.versions.jarPoolCached")}</dt>
          <dd>{statsState.data.count}</dd>
          <dt>{t("admin.versions.jarPoolSize")}</dt>
          <dd>{humanizeBytes(statsState.data.total_bytes)}</dd>
        </dl>
      )}
      <div className="jar-pool-actions">
        <button
          type="button"
          className="btn"
          disabled={gc.isPending}
          onClick={() => setConfirmOpen(true)}
        >
          {gc.isPending
            ? t("admin.versions.gcRunning")
            : t("admin.versions.gc")}
        </button>
      </div>
      <div className="hint">{t("admin.versions.gcHint")}</div>

      <SimpleConfirmDialog
        open={confirmOpen}
        title={t("admin.versions.gcDialog.title")}
        body={t("admin.versions.gcDialog.body")}
        confirmLabel={t("admin.versions.gcDialog.confirm")}
        onConfirm={() => {
          setConfirmOpen(false);
          gc.mutate();
        }}
        onClose={() => setConfirmOpen(false)}
      />
    </div>
  );
}
