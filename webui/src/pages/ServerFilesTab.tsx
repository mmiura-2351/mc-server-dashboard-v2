/**
 * Files tab — two-pane browser + viewer/editor and basic file operations
 * (WEBUI_SPEC.md 6.6).
 *
 * Left pane: a directory listing (entries + a `truncated` notice when the
 * Worker clipped the live listing) with path breadcrumbs. Right pane: a viewer.
 * A text file opens in an editor whose Save issues a versioned base64 `PUT`; a
 * binary file offers download only. Operations: upload (with an "extract ZIP"
 * toggle → `?extract=`), mkdir, rename, delete (typed confirm), download (reuses
 * the authenticated helper in api/download.ts).
 *
 * Permission gating mirrors the API route gates (servers/api/files.py):
 * `file:read` browses/views/downloads/searches, `file:edit` writes/uploads/
 * mkdir/rename/deletes, `file:history` lists versions, `file:rollback` reverts.
 * A 403 routes through onForbidden; other errors toast generically.
 *
 * The typed JSON client has no query-param helper, so the file routes' `?path=`
 * / `?list=` / `?extract=` are appended to the interpolated path as a string and
 * cast to the path type at the call site — the same escape hatch the lifecycle
 * controls use for `?force=`.
 *
 * Search (#451) posts a `{query, by, max_results}` body to `files/search` and
 * renders the matched paths as buttons that open the hit in the viewer (and
 * point the browser at its parent directory). The viewer's History drawer reads
 * `files/history` (a bounded version ring — see the retention hint) and rolls a
 * file back via `files/rollback`, refreshing both the content and the list.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client.ts";
import { downloadFile } from "../api/download.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { Modal } from "../components/Modal.tsx";
import { SimpleConfirmDialog } from "../components/SimpleConfirmDialog.tsx";
import { useToast } from "../components/Toast.tsx";
import { type TranslationKey, t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import {
  decodeBase64Utf8,
  encodeUtf8Base64,
  isProbablyText,
} from "./fileText.ts";
import { atRest, normalizeState } from "./serverState.ts";

type DirListing = components["schemas"]["DirListingResponse"];
type FileContent = components["schemas"]["FileContentResponse"];
type DirEntry = components["schemas"]["DirEntryResponse"];
type ServerResponse = components["schemas"]["ServerResponse"];
type SearchResult = components["schemas"]["SearchResponse"];
type FileVersions = components["schemas"]["FileVersionsResponse"];

/**
 * Map a file-operation error to its toast message. 409 reasons
 * `server_unsettled` and `server_not_stopped` (at-rest-only precondition
 * failures) get an actionable message; everything else falls back to generic.
 */
function fileOperationErrorMessage(error: unknown): TranslationKey {
  if (error instanceof ApiError && error.status === 409) {
    const r = error.reason;
    if (r === "server_unsettled" || r === "server_not_stopped") {
      return "files.error.serverMustBeStopped";
    }
  }
  return "files.error.generic";
}

/** Base `/communities/{cid}/servers/{sid}/files` path for `server`. */
function filesBase(communityId: string, serverId: string): string {
  return apiPath("/api/communities/{community_id}/servers/{server_id}/files", {
    community_id: communityId,
    server_id: serverId,
  });
}

/** Join a directory rel-path and a child name into a POSIX rel-path. */
function joinPath(dir: string, name: string): string {
  return dir === "" ? name : `${dir}/${name}`;
}

/** The directory portion of a rel-path ("" for a top-level file). */
function parentDir(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut === -1 ? "" : path.slice(0, cut);
}

/** Split a rel-path into ordered breadcrumb segments with their cumulative path. */
function breadcrumbs(path: string): { name: string; path: string }[] {
  if (path === "") {
    return [];
  }
  const parts = path.split("/");
  return parts.map((name, i) => ({
    name,
    path: parts.slice(0, i + 1).join("/"),
  }));
}

export function ServerFilesTab({
  server,
  communityId,
  can,
}: {
  server: ServerResponse;
  communityId: string;
  can: Can;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();

  const canRead = can("file:read", { serverId: server.id });
  const canEdit = can("file:edit", { serverId: server.id });
  const notAtRest = !atRest(
    normalizeState(server.observed_state),
    normalizeState(server.desired_state),
  );

  // Current directory rel-path ("" is the working-set root) and the open file.
  const [dir, setDir] = useState("");
  const [openFile, setOpenFile] = useState<string | null>(null);

  const onError = (error: unknown) => {
    if (onForbidden(error)) {
      return;
    }
    showToast(t(fileOperationErrorMessage(error)), "error");
  };

  const listKey = ["files", "list", communityId, server.id, dir];
  const listing = useQuery({
    queryKey: listKey,
    enabled: canRead,
    queryFn: () =>
      api.get(
        `${filesBase(communityId, server.id)}?path=${encodeURIComponent(dir)}&list=true` as never,
      ) as Promise<DirListing>,
  });

  const refetchList = () =>
    queryClient.invalidateQueries({ queryKey: listKey });

  if (!canRead) {
    return <p className="field-error">{t("files.denied")}</p>;
  }

  const enter = (entry: DirEntry) => {
    const next = joinPath(dir, entry.name);
    if (entry.is_dir) {
      setDir(next);
      setOpenFile(null);
    } else {
      setOpenFile(next);
    }
  };

  // Point the browser at the hit's parent directory and open it in the viewer.
  const openHit = (path: string) => {
    setDir(parentDir(path));
    setOpenFile(path);
  };

  return (
    <section className="files">
      {notAtRest && (
        <div className="notice info">{t("files.runningNotice")}</div>
      )}
      <SearchBox
        communityId={communityId}
        serverId={server.id}
        onOpen={openHit}
        onError={onError}
      />
      <Toolbar
        dir={dir}
        communityId={communityId}
        serverId={server.id}
        canEdit={canEdit}
        running={notAtRest}
        onChanged={refetchList}
        onError={onError}
      />
      <Crumbs
        dir={dir}
        onNavigate={(next) => {
          setDir(next);
          setOpenFile(null);
        }}
      />
      <div className="file-layout">
        <div className="card file-tree">
          {listing.isPending ? (
            <p className="sub">{t("files.loading")}</p>
          ) : listing.isError ? (
            <p className="field-error">{t("files.listError")}</p>
          ) : (
            <Listing
              listing={listing.data}
              dir={dir}
              communityId={communityId}
              serverId={server.id}
              canEdit={canEdit}
              openFile={openFile}
              onEnter={enter}
              onChanged={() => {
                refetchList();
                setOpenFile(null);
              }}
              onError={onError}
            />
          )}
        </div>
        <div className="card file-viewer">
          {openFile === null ? (
            <p className="sub">{t("files.noSelection")}</p>
          ) : (
            <Viewer
              key={openFile}
              path={openFile}
              communityId={communityId}
              serverId={server.id}
              canEdit={canEdit}
              can={can}
              running={notAtRest}
              onError={onError}
            />
          )}
        </div>
      </div>
    </section>
  );
}

// ── Breadcrumbs ──────────────────────────────────────────────────────────────

function Crumbs({
  dir,
  onNavigate,
}: {
  dir: string;
  onNavigate: (path: string) => void;
}) {
  return (
    <div className="file-crumbs">
      <button type="button" className="crumb" onClick={() => onNavigate("")}>
        {t("files.root")}
      </button>
      {breadcrumbs(dir).map((crumb) => (
        <span key={crumb.path}>
          {" / "}
          <button
            type="button"
            className="crumb"
            onClick={() => onNavigate(crumb.path)}
          >
            {crumb.name}
          </button>
        </span>
      ))}
    </div>
  );
}

// ── Search box ───────────────────────────────────────────────────────────────

function SearchBox({
  communityId,
  serverId,
  onOpen,
  onError,
}: {
  communityId: string;
  serverId: string;
  onOpen: (path: string) => void;
  onError: (error: unknown) => void;
}) {
  const [query, setQuery] = useState("");
  const [by, setBy] = useState<"name" | "content">("name");

  const search = useMutation({
    mutationFn: () =>
      api.post(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/files/search",
          { community_id: communityId, server_id: serverId },
        ),
        {
          body: JSON.stringify({ query: query.trim(), by, max_results: 100 }),
        },
      ) as Promise<SearchResult>,
    onError,
  });

  const results = search.data;

  return (
    <div className="files-search">
      <form
        className="files-search-row"
        onSubmit={(e) => {
          e.preventDefault();
          search.mutate();
        }}
      >
        <input
          type="search"
          className="files-search-input"
          aria-label={t("files.search.label")}
          placeholder={t("files.search.placeholder")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <label className="files-search-by">
          <input
            type="radio"
            name="search-by"
            checked={by === "name"}
            onChange={() => setBy("name")}
          />
          {t("files.search.byName")}
        </label>
        <label className="files-search-by">
          <input
            type="radio"
            name="search-by"
            checked={by === "content"}
            onChange={() => setBy("content")}
          />
          {t("files.search.byContent")}
        </label>
        <button
          type="submit"
          className="btn sm"
          disabled={query.trim().length === 0 || search.isPending}
        >
          {t("files.search.submit")}
        </button>
      </form>
      {search.isError && (
        <p className="field-error">{t("files.search.error")}</p>
      )}
      {results !== undefined &&
        (results.paths.length === 0 ? (
          <p className="sub">{t("files.search.empty")}</p>
        ) : (
          <>
            {results.truncated && (
              <p className="field-hint">{t("files.search.truncated")}</p>
            )}
            <ul className="files-search-results">
              {results.paths.map((path) => (
                <li key={path}>
                  <button
                    type="button"
                    className="files-search-hit"
                    onClick={() => onOpen(path)}
                  >
                    /{path}
                  </button>
                </li>
              ))}
            </ul>
          </>
        ))}
    </div>
  );
}

// ── Listing pane ─────────────────────────────────────────────────────────────

function Listing({
  listing,
  dir,
  communityId,
  serverId,
  canEdit,
  openFile,
  onEnter,
  onChanged,
  onError,
}: {
  listing: DirListing;
  dir: string;
  communityId: string;
  serverId: string;
  canEdit: boolean;
  openFile: string | null;
  onEnter: (entry: DirEntry) => void;
  onChanged: () => void;
  onError: (error: unknown) => void;
}) {
  const { showToast } = useToast();
  const [renaming, setRenaming] = useState<DirEntry | null>(null);
  const [deleting, setDeleting] = useState<DirEntry | null>(null);

  const remove = useMutation({
    mutationFn: (entry: DirEntry) =>
      api.delete(
        `${filesBase(communityId, serverId)}?path=${encodeURIComponent(joinPath(dir, entry.name))}` as never,
      ),
    onSuccess: () => {
      showToast(t("files.deleted"), "success");
      setDeleting(null);
      onChanged();
    },
    onError: (error) => {
      setDeleting(null);
      onError(error);
    },
  });

  const download = useMutation({
    mutationFn: (entry: DirEntry) =>
      downloadFile(
        `${apiPath(
          "/api/communities/{community_id}/servers/{server_id}/files/download",
          { community_id: communityId, server_id: serverId },
        )}?path=${encodeURIComponent(joinPath(dir, entry.name))}`,
        entry.name,
      ),
    onError,
  });

  if (listing.entries.length === 0) {
    return <p className="sub">{t("files.empty")}</p>;
  }

  return (
    <>
      {listing.truncated && (
        <div className="notice warn">{t("files.truncated")}</div>
      )}
      <ul className="file-list">
        {listing.entries.map((entry) => {
          const full = joinPath(dir, entry.name);
          return (
            <li
              key={entry.name}
              className={`file-row${openFile === full ? " active" : ""}`}
            >
              <button
                type="button"
                className="file-name"
                title={entry.name}
                onClick={() => onEnter(entry)}
              >
                <span aria-hidden="true">{entry.is_dir ? "📁 " : "📄 "}</span>
                {entry.name}
              </button>
              <span className="file-actions">
                {!entry.is_dir && (
                  <button
                    type="button"
                    className="btn sm ghost"
                    onClick={() => download.mutate(entry)}
                  >
                    {t("files.download")}
                  </button>
                )}
                {canEdit && (
                  <button
                    type="button"
                    className="btn sm ghost"
                    onClick={() => setRenaming(entry)}
                  >
                    {t("files.rename")}
                  </button>
                )}
                {canEdit && (
                  <button
                    type="button"
                    className="btn sm ghost danger"
                    onClick={() => setDeleting(entry)}
                  >
                    {t("files.delete")}
                  </button>
                )}
              </span>
            </li>
          );
        })}
      </ul>
      {renaming !== null && (
        <RenameDialog
          entry={renaming}
          dir={dir}
          communityId={communityId}
          serverId={serverId}
          onClose={() => setRenaming(null)}
          onRenamed={() => {
            setRenaming(null);
            onChanged();
          }}
          onError={onError}
        />
      )}
      <SimpleConfirmDialog
        open={deleting !== null}
        title={t("files.delete.dialogTitle")}
        body={t("files.delete.dialogBody")}
        confirmLabel={t("files.delete.confirm")}
        onConfirm={() => deleting !== null && remove.mutate(deleting)}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}

// ── Viewer / editor ──────────────────────────────────────────────────────────

function Viewer({
  path,
  communityId,
  serverId,
  canEdit,
  can,
  running,
  onError,
}: {
  path: string;
  communityId: string;
  serverId: string;
  canEdit: boolean;
  can: Can;
  running: boolean;
  onError: (error: unknown) => void;
}) {
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  // `null` until the user edits, so an untouched view always reflects the
  // server's content even after a save invalidates and refetches it.
  const [draft, setDraft] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);

  const canHistory = can("file:history", { serverId });

  const contentKey = ["files", "content", communityId, serverId, path];
  const content = useQuery({
    queryKey: contentKey,
    queryFn: () =>
      api.get(
        `${filesBase(communityId, serverId)}?path=${encodeURIComponent(path)}` as never,
      ) as Promise<FileContent>,
  });

  const save = useMutation({
    mutationFn: (text: string) =>
      api.put(
        `${filesBase(communityId, serverId)}?path=${encodeURIComponent(path)}` as never,
        { body: JSON.stringify({ content_base64: encodeUtf8Base64(text) }) },
      ),
    onSuccess: () => {
      showToast(t("files.saved"), "success");
      queryClient.invalidateQueries({ queryKey: contentKey });
      setDraft(null);
    },
    onError,
  });

  if (content.isPending) {
    return <p className="sub">{t("files.loading")}</p>;
  }
  if (content.isError) {
    return <p className="field-error">{t("files.openError")}</p>;
  }

  const isText = isProbablyText(content.data.content_base64);
  const downloadName = path.split("/").at(-1) ?? path;

  return (
    <>
      <div className="file-viewer-head">
        <span className="path">/{path}</span>
        <span className="file-viewer-actions">
          <button
            type="button"
            className="btn sm"
            onClick={() =>
              void downloadFile(
                `${apiPath(
                  "/api/communities/{community_id}/servers/{server_id}/files/download",
                  { community_id: communityId, server_id: serverId },
                )}?path=${encodeURIComponent(path)}`,
                downloadName,
              ).catch(onError)
            }
          >
            {t("files.download")}
          </button>
          {canHistory && (
            <button
              type="button"
              className="btn sm ghost"
              onClick={() => setHistoryOpen(true)}
            >
              {t("files.history")}
            </button>
          )}
          {isText && canEdit && (
            <button
              type="button"
              className="btn sm primary"
              disabled={draft === null || save.isPending}
              onClick={() => draft !== null && save.mutate(draft)}
            >
              {t("files.save")}
            </button>
          )}
        </span>
      </div>
      {historyOpen && (
        <HistoryDrawer
          path={path}
          communityId={communityId}
          serverId={serverId}
          canRollback={can("file:rollback", { serverId })}
          onClose={() => setHistoryOpen(false)}
          onRolledBack={() => {
            queryClient.invalidateQueries({ queryKey: contentKey });
            setDraft(null);
          }}
          onError={onError}
        />
      )}
      {isText ? (
        <>
          {running && canEdit && (
            <p className="field-hint">{t("files.runningNotice")}</p>
          )}
          <textarea
            className="file-editor"
            spellCheck={false}
            readOnly={!canEdit}
            aria-label={t("files.editorLabel")}
            value={draft ?? decodeBase64Utf8(content.data.content_base64)}
            onChange={(e) => setDraft(e.target.value)}
          />
        </>
      ) : (
        <p className="sub">{t("files.binary")}</p>
      )}
    </>
  );
}

// ── History drawer + rollback ────────────────────────────────────────────────

function HistoryDrawer({
  path,
  communityId,
  serverId,
  canRollback,
  onClose,
  onRolledBack,
  onError,
}: {
  path: string;
  communityId: string;
  serverId: string;
  canRollback: boolean;
  onClose: () => void;
  onRolledBack: () => void;
  onError: (error: unknown) => void;
}) {
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState<string | null>(null);

  const historyKey = ["files", "history", communityId, serverId, path];
  const history = useQuery({
    queryKey: historyKey,
    queryFn: () =>
      api.get(
        `${filesBase(communityId, serverId)}/history?path=${encodeURIComponent(path)}` as never,
      ) as Promise<FileVersions>,
  });

  const rollback = useMutation({
    mutationFn: (versionId: string) =>
      api.post(
        `${filesBase(communityId, serverId)}/rollback?path=${encodeURIComponent(path)}` as never,
        { body: JSON.stringify({ version_id: versionId }) },
      ),
    onSuccess: () => {
      showToast(t("files.rolledBack"), "success");
      // The rolled-over current is itself retained, so refresh the list too.
      queryClient.invalidateQueries({ queryKey: historyKey });
      onRolledBack();
      setConfirming(null);
    },
    onError: (error) => {
      setConfirming(null);
      onError(error);
    },
  });

  return (
    <Modal
      open
      title={t("files.history.title")}
      onClose={onClose}
      footer={
        <button type="button" className="btn ghost" onClick={onClose}>
          {t("files.history.close")}
        </button>
      }
    >
      <p className="field-hint">{t("files.history.hint")}</p>
      {history.isPending ? (
        <p className="sub">{t("files.history.loading")}</p>
      ) : history.isError ? (
        <p className="field-error">{t("files.history.error")}</p>
      ) : history.data.versions.length === 0 ? (
        <p className="sub">{t("files.history.empty")}</p>
      ) : (
        <ul className="files-history-list">
          {history.data.versions.map((versionId) => (
            <li key={versionId} className="files-history-row">
              <span className="files-history-id">{versionId}</span>
              {canRollback && (
                <button
                  type="button"
                  className="btn sm ghost"
                  onClick={() => setConfirming(versionId)}
                >
                  {t("files.history.rollback")}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {confirming !== null && (
        <Modal
          open
          title={t("files.rollback.dialogTitle")}
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
                className="btn danger"
                disabled={rollback.isPending}
                onClick={() => rollback.mutate(confirming)}
              >
                {t("files.rollback.confirm")}
              </button>
            </>
          }
        >
          <p>{t("files.rollback.dialogBody")}</p>
          <p className="files-history-id">{confirming}</p>
        </Modal>
      )}
    </Modal>
  );
}

// ── Toolbar: upload + mkdir ──────────────────────────────────────────────────

function Toolbar({
  dir,
  communityId,
  serverId,
  canEdit,
  running,
  onChanged,
  onError,
}: {
  dir: string;
  communityId: string;
  serverId: string;
  canEdit: boolean;
  running: boolean;
  onChanged: () => void;
  onError: (error: unknown) => void;
}) {
  const MAX_UPLOAD_BYTES = 512 * 1024 * 1024;
  const { showToast } = useToast();
  const [mkdirOpen, setMkdirOpen] = useState(false);
  const [extract, setExtract] = useState(false);

  const upload = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api.postForm(
        `${apiPath(
          "/api/communities/{community_id}/servers/{server_id}/files/upload",
          { community_id: communityId, server_id: serverId },
        )}?path=${encodeURIComponent(dir)}&extract=${extract}` as never,
        form,
      );
    },
    onSuccess: () => {
      showToast(t("files.uploaded"), "success");
      onChanged();
    },
    onError,
  });

  if (!canEdit) {
    return null;
  }

  const atRestTooltip = running
    ? t("files.error.serverMustBeStopped")
    : undefined;

  return (
    <div className="toolbar-row files-toolbar">
      <label className="files-extract">
        <input
          type="checkbox"
          checked={extract}
          onChange={(e) => setExtract(e.target.checked)}
        />
        {t("files.extractZip")}
      </label>
      {running ? (
        <button
          type="button"
          className="btn sm file-upload"
          disabled
          title={atRestTooltip}
        >
          {t("files.upload")}
        </button>
      ) : (
        <label className="btn sm file-upload">
          {t("files.upload")}
          <input
            type="file"
            hidden
            aria-label={t("files.upload")}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file !== undefined) {
                if (file.size > MAX_UPLOAD_BYTES) {
                  showToast(t("files.error.tooLarge"), "error");
                } else {
                  upload.mutate(file);
                }
              }
              e.target.value = "";
            }}
          />
        </label>
      )}
      <button
        type="button"
        className="btn sm"
        disabled={running}
        title={atRestTooltip}
        onClick={() => setMkdirOpen(true)}
      >
        {t("files.newFolder")}
      </button>
      {mkdirOpen && (
        <MkdirDialog
          dir={dir}
          communityId={communityId}
          serverId={serverId}
          onClose={() => setMkdirOpen(false)}
          onCreated={() => {
            setMkdirOpen(false);
            onChanged();
          }}
          onError={onError}
        />
      )}
    </div>
  );
}

// ── Mkdir / rename prompt dialogs ────────────────────────────────────────────

function MkdirDialog({
  dir,
  communityId,
  serverId,
  onClose,
  onCreated,
  onError,
}: {
  dir: string;
  communityId: string;
  serverId: string;
  onClose: () => void;
  onCreated: () => void;
  onError: (error: unknown) => void;
}) {
  const { showToast } = useToast();
  const [name, setName] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.post(
        `${apiPath(
          "/api/communities/{community_id}/servers/{server_id}/files/directories",
          { community_id: communityId, server_id: serverId },
        )}?path=${encodeURIComponent(joinPath(dir, name.trim()))}` as never,
      ),
    onSuccess: () => {
      showToast(t("files.folderCreated"), "success");
      onCreated();
    },
    onError: (error) => {
      onClose();
      onError(error);
    },
  });

  return (
    <PromptDialog
      title={t("files.newFolder")}
      label={t("files.folderName")}
      value={name}
      onChange={setName}
      confirmLabel={t("files.create")}
      onConfirm={() => create.mutate()}
      onClose={onClose}
    />
  );
}

function RenameDialog({
  entry,
  dir,
  communityId,
  serverId,
  onClose,
  onRenamed,
  onError,
}: {
  entry: DirEntry;
  dir: string;
  communityId: string;
  serverId: string;
  onClose: () => void;
  onRenamed: () => void;
  onError: (error: unknown) => void;
}) {
  const { showToast } = useToast();
  const [name, setName] = useState(entry.name);

  const rename = useMutation({
    mutationFn: () =>
      api.post(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/files/rename",
          { community_id: communityId, server_id: serverId },
        ),
        {
          body: JSON.stringify({
            from: joinPath(dir, entry.name),
            to: joinPath(dir, name.trim()),
          }),
        },
      ),
    onSuccess: () => {
      showToast(t("files.renamed"), "success");
      onRenamed();
    },
    onError: (error) => {
      onClose();
      onError(error);
    },
  });

  return (
    <PromptDialog
      title={t("files.rename")}
      label={t("files.newName")}
      value={name}
      onChange={setName}
      confirmLabel={t("files.rename")}
      onConfirm={() => rename.mutate()}
      onClose={onClose}
    />
  );
}

/** A minimal single-text-field modal for mkdir / rename, on the shared Modal. */
function PromptDialog({
  title,
  label,
  value,
  onChange,
  confirmLabel,
  onConfirm,
  onClose,
}: {
  title: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  confirmLabel: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Modal
      open
      title={title}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={value.trim().length === 0}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </>
      }
    >
      <label className="field">
        {label}
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      </label>
    </Modal>
  );
}
