/**
 * Mod library management page (issue #1266).
 *
 * Lists all library mods, with upload (multipart, optional side override),
 * download (authenticated blob), delete (typed-confirm), and a Modrinth
 * search/import modal. Mirrors the resource pack library page
 * (ResourcePacksPage). Available to all authenticated users; delete is visible
 * only to the uploader or platform admins.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client.ts";
import { downloadFile } from "../api/download.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { useCurrentUser } from "../auth/useCurrentUser.ts";
import { ConfirmDialog } from "../components/ConfirmDialog.tsx";
import { FilePicker } from "../components/FilePicker.tsx";
import { Modal } from "../components/Modal.tsx";
import { useToast } from "../components/Toast.tsx";
import { formatDateTime, shortId } from "../format.ts";
import { type TranslationKey, t } from "../i18n/index.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";

type ModResponse = components["schemas"]["ModResponse"];
type CatalogSearchHit = components["schemas"]["CatalogSearchHitResponse"];
type CatalogVersion = components["schemas"]["CatalogVersionResponse"];

const MODS_KEY = ["mods"] as const;

// The library `side` axis (issue #1258); rendered as a badge with a localized
// label. Anything outside the known set falls back to the raw value.
const SIDE_LABEL: Record<string, TranslationKey> = {
  server: "mods.side.server",
  client: "mods.side.client",
  both: "mods.side.both",
};

function SideBadge({ side }: { side: string }) {
  const key = SIDE_LABEL[side];
  return <span className="badge">{key !== undefined ? t(key) : side}</span>;
}

export function ModsPage() {
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const onForbidden = useOnForbidden();
  const currentUser = useCurrentUser();
  const isAdmin = currentUser.data?.is_platform_admin === true;
  const userId = currentUser.data?.id;

  const [uploadOpen, setUploadOpen] = useState(false);
  const [browseOpen, setBrowseOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ModResponse | null>(null);

  const listQuery = useQuery({
    queryKey: MODS_KEY,
    queryFn: () => api.get("/api/mods"),
  });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: MODS_KEY });
  };

  const remove = useMutation({
    mutationFn: (mod: ModResponse) =>
      api.delete(apiPath("/api/mods/{mod_id}", { mod_id: mod.id })),
    onSuccess: () => {
      showToast(t("mods.deleted"), "success");
      refresh();
    },
    onError: (error) => {
      if (onForbidden(error)) return;
      if (error instanceof ApiError && error.reason === "mod_in_use") {
        showToast(t("mods.error.inUse"), "error");
      } else {
        showToast(t("mods.error.deleteFailed"), "error");
      }
    },
  });

  const download = useMutation({
    mutationFn: (mod: ModResponse) =>
      downloadFile(
        apiPath("/api/mods/{mod_id}/download", { mod_id: mod.id }),
        mod.filename,
      ),
    onError: (error) => {
      if (onForbidden(error)) return;
      showToast(t("mods.error.downloadFailed"), "error");
    },
  });

  function canDelete(mod: ModResponse): boolean {
    return isAdmin || mod.uploaded_by === userId;
  }

  return (
    <div className="mods">
      <div className="page-head">
        <div>
          <h1>{t("page.mods")}</h1>
          <div className="sub">{t("mods.subtitle")}</div>
        </div>
        <div className="actions">
          <button
            type="button"
            className="btn"
            onClick={() => setBrowseOpen(true)}
          >
            {t("mods.browse")}
          </button>
          <button
            type="button"
            className="btn primary"
            onClick={() => setUploadOpen(true)}
          >
            {t("mods.upload")}
          </button>
        </div>
      </div>

      {listQuery.isPending ? (
        <p className="sub" role="status">
          {t("mods.loading")}
        </p>
      ) : listQuery.isError ? (
        <p className="field-error" role="alert">
          {t("mods.loadError")}
        </p>
      ) : (
        <div className="card table-card">
          {listQuery.data.mods.length === 0 ? (
            <p className="sub">{t("mods.empty")}</p>
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>{t("mods.col.displayName")}</th>
                  <th>{t("mods.col.version")}</th>
                  <th>{t("mods.col.loader")}</th>
                  <th>{t("mods.col.mcVersions")}</th>
                  <th>{t("mods.col.side")}</th>
                  <th>{t("mods.col.uploaded")}</th>
                  <th>{t("mods.col.uploader")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {listQuery.data.mods.map((mod) => (
                  <tr key={mod.id}>
                    <td>{mod.display_name}</td>
                    <td>{mod.version_number}</td>
                    <td>{mod.loader_type}</td>
                    <td>{mod.mc_versions.join(", ")}</td>
                    <td>
                      <SideBadge side={mod.side} />
                    </td>
                    <td>{formatDateTime(mod.created_at)}</td>
                    <td title={mod.uploaded_by}>{shortId(mod.uploaded_by)}</td>
                    <td className="row-actions">
                      <button
                        type="button"
                        className="btn sm"
                        onClick={() => download.mutate(mod)}
                      >
                        {t("mods.download")}
                      </button>
                      {canDelete(mod) && (
                        <button
                          type="button"
                          className="btn sm danger"
                          onClick={() => setDeleteTarget(mod)}
                        >
                          {t("mods.delete")}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {uploadOpen && (
        <UploadDialog
          onSuccess={() => {
            setUploadOpen(false);
            showToast(t("mods.uploaded"), "success");
            refresh();
          }}
          onClose={() => setUploadOpen(false)}
        />
      )}

      {browseOpen && (
        <BrowseDialog
          onImported={() => {
            showToast(t("mods.browseDialog.imported"), "success");
            refresh();
          }}
          onClose={() => setBrowseOpen(false)}
        />
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title={t("mods.deleteDialog.title")}
        body={t("mods.deleteDialog.body")}
        confirmPhrase={deleteTarget?.display_name ?? ""}
        confirmLabel={t("mods.deleteDialog.confirm")}
        promptLabel={t("mods.deleteDialog.prompt")}
        onConfirm={() => {
          const target = deleteTarget;
          setDeleteTarget(null);
          if (target !== null) {
            remove.mutate(target);
          }
        }}
        onClose={() => setDeleteTarget(null)}
      />
    </div>
  );
}

function UploadDialog({
  onSuccess,
  onClose,
}: {
  onSuccess: () => void;
  onClose: () => void;
}) {
  const MAX_UPLOAD_BYTES = 256 * 1024 * 1024;
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const [displayName, setDisplayName] = useState("");
  const [side, setSide] = useState("");
  const [file, setFile] = useState<File | null>(null);

  const upload = useMutation({
    mutationFn: ({ name, f }: { name: string; f: File }) => {
      const form = new FormData();
      form.append("display_name", name);
      form.append("file", f);
      // The side override is optional: omit it so the backend keeps the
      // manifest's auto-detected value (defaults to `both`, issue #1258).
      if (side !== "") {
        form.append("side", side);
      }
      return api.postForm("/api/mods", form);
    },
    onSuccess,
    onError: (error) => {
      if (onForbidden(error)) return;
      showToast(t("mods.error.uploadFailed"), "error");
    },
  });

  const nameEmpty = displayName.trim() === "";

  return (
    <Modal
      open={true}
      title={t("mods.uploadDialog.title")}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={nameEmpty || file === null || upload.isPending}
            onClick={() => {
              if (!nameEmpty && file !== null) {
                if (file.size > MAX_UPLOAD_BYTES) {
                  showToast(t("mods.error.tooLarge"), "error");
                  return;
                }
                upload.mutate({ name: displayName.trim(), f: file });
              }
            }}
          >
            {upload.isPending
              ? t("mods.uploadDialog.uploading")
              : t("mods.uploadDialog.submit")}
          </button>
        </>
      }
    >
      <label className="field">
        {t("mods.uploadDialog.displayName")}
        <input
          type="text"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
        />
      </label>
      <label className="field">
        {t("mods.uploadDialog.side")}
        <select value={side} onChange={(e) => setSide(e.target.value)}>
          <option value="">{t("mods.uploadDialog.sideAuto")}</option>
          <option value="server">{t("mods.side.server")}</option>
          <option value="client">{t("mods.side.client")}</option>
          <option value="both">{t("mods.side.both")}</option>
        </select>
      </label>
      <label className="field" htmlFor="mod-upload-file">
        {t("mods.uploadDialog.file")}
      </label>
      <FilePicker
        id="mod-upload-file"
        accept=".jar"
        file={file}
        onSelect={setFile}
      />
    </Modal>
  );
}

// Build the Modrinth search URL, carrying the optional loader/game-version
// facets, and cast back to the literal schema path so the typed client keeps
// the generated response typing (mirrors AdminUsersPage's usersUrl).
function searchUrl(
  query: string,
  loader: string,
  gameVersion: string,
): "/api/catalog/search" {
  const params = new URLSearchParams({ query });
  if (loader !== "") params.set("loader", loader);
  if (gameVersion !== "") params.set("game_version", gameVersion);
  return `/api/catalog/search?${params.toString()}` as "/api/catalog/search";
}

function BrowseDialog({
  onImported,
  onClose,
}: {
  onImported: () => void;
  onClose: () => void;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const [query, setQuery] = useState("");
  const [loader, setLoader] = useState("");
  const [gameVersion, setGameVersion] = useState("");
  // Holds the query that was actually submitted; the search runs only after the
  // user clicks Search so typing does not fire a request per keystroke.
  const [submitted, setSubmitted] = useState<{
    query: string;
    loader: string;
    gameVersion: string;
  } | null>(null);
  // A selected search hit drills into its project detail (versions). Import is
  // per-version, so the user picks a concrete version to bring into the library.
  const [selected, setSelected] = useState<CatalogSearchHit | null>(null);

  const searchQuery = useQuery({
    queryKey: ["catalog-search", submitted],
    queryFn: () => {
      const params = submitted ?? { query: "", loader: "", gameVersion: "" };
      return api.get(
        searchUrl(params.query, params.loader, params.gameVersion),
      );
    },
    enabled: submitted !== null,
  });

  const projectQuery = useQuery({
    queryKey: ["catalog-project", selected?.project_id],
    queryFn: () =>
      api.get(
        apiPath("/api/catalog/projects/{project_id}", {
          project_id: selected?.project_id ?? "",
        }),
      ),
    enabled: selected !== null,
  });

  const importMod = useMutation({
    mutationFn: (version: CatalogVersion) =>
      api.post("/api/mods/import", {
        body: JSON.stringify({
          project_id: version.project_id,
          version_id: version.version_id,
        }),
      }),
    onSuccess: onImported,
    onError: (error) => {
      if (onForbidden(error)) return;
      showToast(t("mods.browseDialog.importFailed"), "error");
    },
  });

  const search = () => {
    const trimmed = query.trim();
    if (trimmed !== "") {
      setSelected(null);
      setSubmitted({ query: trimmed, loader, gameVersion });
    }
  };

  return (
    <Modal
      open={true}
      title={t("mods.browseDialog.title")}
      onClose={onClose}
      footer={
        <button type="button" className="btn ghost" onClick={onClose}>
          {t("common.cancel")}
        </button>
      }
    >
      {selected !== null ? (
        <div className="catalog-detail">
          <button
            type="button"
            className="btn sm ghost"
            onClick={() => setSelected(null)}
          >
            {t("mods.browseDialog.back")}
          </button>
          <h3>{selected.title}</h3>
          <div className="sub">{t("mods.browseDialog.versions")}</div>
          {projectQuery.isPending ? (
            <p className="sub" role="status">
              {t("mods.browseDialog.loadingProject")}
            </p>
          ) : projectQuery.isError ? (
            <p className="field-error" role="alert">
              {t("mods.browseDialog.projectFailed")}
            </p>
          ) : projectQuery.data.versions.length === 0 ? (
            <p className="sub">{t("mods.browseDialog.noVersions")}</p>
          ) : (
            <ul className="catalog-results">
              {projectQuery.data.versions.map((version) => (
                <li key={version.version_id} className="catalog-result">
                  <div>
                    <div className="catalog-title">{version.name}</div>
                    <div className="sub">
                      {version.version_number} ·{" "}
                      {version.game_versions.join(", ")}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="btn sm"
                    disabled={importMod.isPending}
                    onClick={() => importMod.mutate(version)}
                  >
                    {importMod.isPending
                      ? t("mods.browseDialog.importing")
                      : t("mods.browseDialog.import")}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <>
          <label className="field">
            {t("mods.browseDialog.query")}
            <input
              type="text"
              value={query}
              placeholder={t("mods.browseDialog.queryPlaceholder")}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
          <label className="field">
            {t("mods.browseDialog.loader")}
            <select value={loader} onChange={(e) => setLoader(e.target.value)}>
              <option value="">{t("mods.browseDialog.any")}</option>
              <option value="fabric">fabric</option>
              <option value="forge">forge</option>
              <option value="neoforge">neoforge</option>
              <option value="quilt">quilt</option>
              <option value="paper">paper</option>
            </select>
          </label>
          <label className="field">
            {t("mods.browseDialog.gameVersion")}
            <input
              type="text"
              value={gameVersion}
              onChange={(e) => setGameVersion(e.target.value)}
            />
          </label>
          <button
            type="button"
            className="btn primary"
            disabled={query.trim() === ""}
            onClick={search}
          >
            {t("mods.browseDialog.search")}
          </button>

          {submitted !== null &&
            (searchQuery.isPending ? (
              <p className="sub" role="status">
                {t("mods.browseDialog.searching")}
              </p>
            ) : searchQuery.isError ? (
              <p className="field-error" role="alert">
                {t("mods.browseDialog.searchFailed")}
              </p>
            ) : searchQuery.data.hits.length === 0 ? (
              <p className="sub">{t("mods.browseDialog.empty")}</p>
            ) : (
              <ul className="catalog-results">
                {searchQuery.data.hits.map((hit) => (
                  <li key={hit.project_id} className="catalog-result">
                    <div>
                      <div className="catalog-title">{hit.title}</div>
                      <div className="sub">{hit.description}</div>
                    </div>
                    <button
                      type="button"
                      className="btn sm"
                      onClick={() => setSelected(hit)}
                    >
                      {t("mods.browseDialog.import")}
                    </button>
                  </li>
                ))}
              </ul>
            ))}
        </>
      )}
    </Modal>
  );
}
