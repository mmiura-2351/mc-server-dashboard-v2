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

// Platform admin Versions page (WEBUI_SPEC.md 6.12): per-type catalog freshness,
// refresh (all or one type), and the shared JAR pool stats + GC. The catalog
// endpoints carry no freshness timestamp — GET /versions returns only the type
// names and GET /versions/{type} only the ordered version list (newest-first per
// the adapters) — so this renders version count + latest, not an invented
// "refreshed at" column (#478).

export function AdminVersionsPage() {
  const typesQuery = useQuery({
    queryKey: ["versions", "types"],
    queryFn: ({ signal }) => api.get("/api/versions", { signal }),
  });

  const types = typesQuery.data?.server_types ?? [];

  return (
    <div className="admin-versions">
      <div className="page-head">
        <div>
          <h1>{t("page.adminVersions")}</h1>
          <div className="sub">{t("admin.versions.subtitle")}</div>
        </div>
      </div>

      {typesQuery.isPending ? (
        <p className="sub" role="status">
          {t("admin.versions.loading")}
        </p>
      ) : typesQuery.isError ? (
        <p className="field-error" role="alert">
          {t("admin.versions.loadError")}
        </p>
      ) : (
        <Catalog types={types} />
      )}

      <JarPool />
    </div>
  );
}

function Catalog({ types }: { types: string[] }) {
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  // Per-type version lists feed the count + latest cells. They are independent of
  // the type list query, so a refresh invalidates them by key.
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
      // The catalog cache is invalidated server-side; drop the local copies so the
      // next read refetches. Scope to the catalog keys (`["versions","types"]` and
      // the per-type `["versions", <type>]`) so the JAR-pool stats key
      // (`["versions","jar-pool","stats"]`) is left to GC's own invalidation.
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

  // The button busy state is per target: "all" while refreshing all, or the type
  // name while refreshing one, so only the clicked button shows a busy label.
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
      {statsQuery.isPending ? (
        <p className="sub" role="status">
          {t("admin.versions.loading")}
        </p>
      ) : statsQuery.isError ? (
        <p className="field-error" role="alert">
          {t("admin.versions.loadError")}
        </p>
      ) : (
        <dl className="kv">
          <dt>{t("admin.versions.jarPoolCached")}</dt>
          <dd>{statsQuery.data?.count ?? 0}</dd>
          <dt>{t("admin.versions.jarPoolSize")}</dt>
          <dd>{humanizeBytes(statsQuery.data?.total_bytes ?? 0)}</dd>
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
