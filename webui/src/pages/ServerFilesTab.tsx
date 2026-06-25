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
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, postFormWithProgress } from "../api/client.ts";
import { downloadFile } from "../api/download.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { Modal } from "../components/Modal.tsx";
import { SimpleConfirmDialog } from "../components/SimpleConfirmDialog.tsx";
import { useToast } from "../components/Toast.tsx";
import { UploadProgress } from "../components/UploadProgress.tsx";
import { useUploadProgress } from "../components/useUploadProgress.ts";
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
 * failures) get an actionable message; `content_dir_protected` is handled
 * separately (inline notice, not a toast); everything else falls back to
 * generic.
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

/** True when the error is a 409 content_dir_protected rejection. */
function isContentDirProtected(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    error.reason === "content_dir_protected"
  );
}

/** The loader-aware noun for the managed-content tab (Mods vs Plugins). */
function contentTabNoun(serverType: string): string {
  return serverType === "fabric" || serverType === "forge"
    ? t("serverDetail.tab.mods")
    : t("serverDetail.tab.plugins");
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
  const [contentDirNotice, setContentDirNotice] = useState(false);

  // Multi-select: track selected file paths and the last-clicked index for
  // shift-click range selection. Selection clears on directory change.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const lastClickedIdx = useRef<number | null>(null);

  /** Wrapper around setDir that clears the selection state. */
  const navigateDir = (next: string) => {
    setDir(next);
    setSelected(new Set());
    lastClickedIdx.current = null;
  };

  const onError = (error: unknown) => {
    if (onForbidden(error)) {
      return;
    }
    if (isContentDirProtected(error)) {
      setContentDirNotice(true);
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

  const serverId = server.id;

  // Upload state lifted from Toolbar so the drop zone can share it.
  const MAX_UPLOAD_BYTES = 512 * 1024 * 1024;
  const [extract, setExtract] = useState(false);
  const progress = useUploadProgress();

  const upload = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      progress.start(file.size);
      return postFormWithProgress(
        `${apiPath(
          "/api/communities/{community_id}/servers/{server_id}/files/upload",
          { community_id: communityId, server_id: serverId },
        )}?path=${encodeURIComponent(dir)}&extract=${extract}` as never,
        form,
        progress.onProgress,
      );
    },
    onSuccess: () => {
      progress.reset();
      showToast(t("files.uploaded"), "success");
      refetchList();
    },
    onError: (error) => {
      progress.reset();
      onError(error);
    },
  });

  // Sequential upload for multiple files (e.g. drag-and-drop).
  const uploadFiles = useCallback(
    async (files: File[]) => {
      for (const file of files) {
        if (file.size > MAX_UPLOAD_BYTES) {
          showToast(t("files.error.tooLarge"), "error");
          continue;
        }
        await upload.mutateAsync(file);
      }
    },
    [upload, showToast],
  );

  /** Move files via POST /files/rename. Handles multi-select. */
  const moveFiles = async (paths: string[], destDir: string) => {
    let movedAny = false;
    for (const from of paths) {
      const name = from.split("/").at(-1) ?? from;
      const to = destDir === "" ? name : `${destDir}/${name}`;
      if (from === to) continue;
      try {
        await api.post(
          apiPath(
            "/api/communities/{community_id}/servers/{server_id}/files/rename",
            { community_id: communityId, server_id: serverId },
          ),
          { body: JSON.stringify({ from, to }) },
        );
        movedAny = true;
      } catch (error) {
        if (
          error instanceof ApiError &&
          error.status === 409 &&
          error.reason === "destination_exists"
        ) {
          showToast(t("files.error.moveConflict", { name }), "error");
        } else {
          onError(error);
        }
      }
    }
    if (movedAny) {
      showToast(t("files.moved"), "success");
      refetchList();
      setSelected(new Set());
    }
  };

  // Drag-and-drop state for the file-tree drop zone.
  const dragCounter = useRef(0);
  const [dragOver, setDragOver] = useState(false);
  const dropEnabled = canEdit && !notAtRest;

  const onDragEnter = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      if (
        !dropEnabled ||
        e.dataTransfer.types.includes("application/x-file-move")
      )
        return;
      dragCounter.current += 1;
      if (dragCounter.current === 1) setDragOver(true);
    },
    [dropEnabled],
  );

  const onDragLeave = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      if (
        !dropEnabled ||
        e.dataTransfer.types.includes("application/x-file-move")
      )
        return;
      dragCounter.current -= 1;
      if (dragCounter.current === 0) setDragOver(false);
    },
    [dropEnabled],
  );

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
  }, []);

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      dragCounter.current = 0;
      setDragOver(false);
      if (!dropEnabled) return;
      // Ignore internal file-move drags — those are handled by folder/crumb targets.
      if (e.dataTransfer.types.includes("application/x-file-move")) return;
      const files = Array.from(e.dataTransfer.files);
      if (files.length > 0) {
        void uploadFiles(files);
      }
    },
    [dropEnabled, uploadFiles],
  );

  // Keyboard-triggered delete/rename (issue #1465). These are separate from
  // the inline button flows in Listing so the parent can drive them from
  // keydown without refactoring Listing's internal state.
  const [kbDeleteOpen, setKbDeleteOpen] = useState(false);
  const [kbRenameEntry, setKbRenameEntry] = useState<DirEntry | null>(null);

  const kbDelete = useMutation({
    mutationFn: async () => {
      const paths = Array.from(selected);
      for (const path of paths) {
        await api.delete(
          `${filesBase(communityId, serverId)}?path=${encodeURIComponent(path)}` as never,
        );
      }
    },
    onSuccess: () => {
      showToast(t("files.deleted"), "success");
      setKbDeleteOpen(false);
      setSelected(new Set());
      refetchList();
      setOpenFile(null);
    },
    onError: (error) => {
      setKbDeleteOpen(false);
      onError(error);
      refetchList();
    },
  });

  // Keyboard shortcuts: Delete/Backspace, F2, Ctrl+A, Escape.
  const containerRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // Skip when a text input / textarea / contenteditable is focused — the
      // user is typing, not issuing a file command.
      const tag = (e.target as HTMLElement).tagName;
      if (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        (e.target as HTMLElement).isContentEditable
      ) {
        return;
      }

      if (e.key === "Delete" || e.key === "Backspace") {
        if (selected.size > 0 && canEdit && !notAtRest) {
          e.preventDefault();
          setKbDeleteOpen(true);
        }
        return;
      }

      if (e.key === "F2") {
        if (selected.size === 1 && canEdit && !notAtRest && listing.data) {
          e.preventDefault();
          const selectedPath = Array.from(selected)[0];
          const name = selectedPath.split("/").at(-1) ?? selectedPath;
          const entry = listing.data.entries.find((en) => en.name === name);
          if (entry) {
            setKbRenameEntry(entry);
          }
        }
        return;
      }

      if (e.key === "a" && (e.ctrlKey || e.metaKey)) {
        if (listing.data) {
          e.preventDefault();
          setSelected(
            new Set(listing.data.entries.map((en) => joinPath(dir, en.name))),
          );
        }
        return;
      }

      if (e.key === "Escape") {
        if (selected.size > 0) {
          setSelected(new Set());
        }
      }
    };

    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [selected, canEdit, notAtRest, listing.data, dir]);

  if (!canRead) {
    return <p className="field-error">{t("files.denied")}</p>;
  }

  const enter = (entry: DirEntry) => {
    const next = joinPath(dir, entry.name);
    if (entry.is_dir) {
      navigateDir(next);
      setOpenFile(null);
    } else {
      setOpenFile(next);
    }
  };

  // Point the browser at the hit's parent directory and open it in the viewer.
  const openHit = (path: string) => {
    navigateDir(parentDir(path));
    setOpenFile(path);
  };

  return (
    <section className="files" ref={containerRef}>
      {notAtRest && (
        <div className="notice info">{t("files.runningNotice")}</div>
      )}
      {contentDirNotice && (
        <ContentDirNotice
          serverType={server.server_type}
          onDismiss={() => setContentDirNotice(false)}
        />
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
        serverId={serverId}
        canEdit={canEdit}
        running={notAtRest}
        extract={extract}
        setExtract={setExtract}
        upload={upload}
        onChanged={refetchList}
        onError={onError}
        selected={selected}
        totalCount={listing.data?.entries.length ?? 0}
        onSelectAll={() => {
          if (listing.data) {
            setSelected(
              new Set(listing.data.entries.map((e) => joinPath(dir, e.name))),
            );
          }
        }}
        onDeselectAll={() => setSelected(new Set())}
        onClearSelection={() => setSelected(new Set())}
      />
      <Crumbs
        dir={dir}
        onNavigate={(next) => {
          navigateDir(next);
          setOpenFile(null);
        }}
        dropEnabled={dropEnabled}
        onMoveTo={moveFiles}
      />
      {progress.active && (
        <UploadProgress
          loaded={progress.loaded}
          total={progress.total}
          percent={progress.percent}
          elapsedMs={progress.elapsedMs}
        />
      )}
      <div className="file-layout">
        {/* biome-ignore lint/a11y/noStaticElementInteractions: drop zone uses drag events only; keyboard upload is via the toolbar button */}
        <div
          className={`card file-tree${dragOver ? " drop-zone-active" : ""}`}
          onDragEnter={onDragEnter}
          onDragLeave={onDragLeave}
          onDragOver={onDragOver}
          onDrop={onDrop}
        >
          {dragOver && (
            <div className="drop-zone-overlay">
              <span>{t("files.dropZone")}</span>
            </div>
          )}
          {listing.isPending ? (
            <p className="sub">{t("files.loading")}</p>
          ) : listing.isError ? (
            <p className="field-error">{t("files.listError")}</p>
          ) : (
            <Listing
              listing={listing.data}
              dir={dir}
              communityId={communityId}
              serverId={serverId}
              canEdit={canEdit}
              running={notAtRest}
              openFile={openFile}
              onEnter={enter}
              onChanged={() => {
                refetchList();
                setOpenFile(null);
              }}
              onError={onError}
              selected={selected}
              onSelectionChange={setSelected}
              lastClickedIdx={lastClickedIdx}
              dropEnabled={dropEnabled}
              onMoveTo={moveFiles}
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
      <SimpleConfirmDialog
        open={kbDeleteOpen}
        title={t("files.delete.dialogTitle")}
        body={
          selected.size > 1
            ? t("files.bulk.delete.dialogBody", { count: selected.size })
            : t("files.delete.dialogBody")
        }
        confirmLabel={t("files.delete.confirm")}
        onConfirm={() => kbDelete.mutate()}
        onClose={() => setKbDeleteOpen(false)}
      />
      {kbRenameEntry !== null && (
        <RenameDialog
          entry={kbRenameEntry}
          dir={dir}
          communityId={communityId}
          serverId={serverId}
          onClose={() => setKbRenameEntry(null)}
          onRenamed={() => {
            setKbRenameEntry(null);
            refetchList();
          }}
          onError={onError}
        />
      )}
    </section>
  );
}

// ── Breadcrumbs ──────────────────────────────────────────────────────────────

function Crumbs({
  dir,
  onNavigate,
  dropEnabled,
  onMoveTo,
}: {
  dir: string;
  onNavigate: (path: string) => void;
  dropEnabled: boolean;
  onMoveTo: (paths: string[], destDir: string) => Promise<void>;
}) {
  const [dropTarget, setDropTarget] = useState<string | null>(null);

  const crumbDrop = (targetDir: string) => ({
    onDragOver: (e: React.DragEvent) => {
      if (
        !dropEnabled ||
        !e.dataTransfer.types.includes("application/x-file-move")
      )
        return;
      e.preventDefault();
      setDropTarget(targetDir);
    },
    onDragLeave: () => setDropTarget(null),
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setDropTarget(null);
      if (!dropEnabled) return;
      const raw = e.dataTransfer.getData("application/x-file-move");
      if (!raw) return;
      const paths = JSON.parse(raw) as string[];
      void onMoveTo(paths, targetDir);
    },
  });

  return (
    <div className="file-crumbs">
      <button
        type="button"
        className={`crumb${dropTarget === "" ? " drop-target" : ""}`}
        onClick={() => onNavigate("")}
        {...crumbDrop("")}
      >
        {t("files.root")}
      </button>
      {breadcrumbs(dir).map((crumb) => (
        <span key={crumb.path}>
          {" / "}
          <button
            type="button"
            className={`crumb${dropTarget === crumb.path ? " drop-target" : ""}`}
            onClick={() => onNavigate(crumb.path)}
            {...crumbDrop(crumb.path)}
          >
            {crumb.name}
          </button>
        </span>
      ))}
    </div>
  );
}

// ── Content-dir redirect notice ─────────────────────────────────────────────

function ContentDirNotice({
  serverType,
  onDismiss,
}: {
  serverType: string;
  onDismiss: () => void;
}) {
  const noun = contentTabNoun(serverType);
  const message = t("files.error.contentDirProtected").replace(
    /\{noun\}/g,
    noun,
  );
  const linkLabel = t("files.error.goToContentTab").replace(/\{noun\}/g, noun);

  return (
    <div className="notice warn" role="alert">
      <span>{message}</span> <a href="#plugins">{linkLabel}</a>
      <button
        type="button"
        className="btn sm ghost"
        onClick={onDismiss}
        aria-label={t("common.close")}
      >
        {t("common.close")}
      </button>
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
  running,
  openFile,
  onEnter,
  onChanged,
  onError,
  selected,
  onSelectionChange,
  lastClickedIdx,
  dropEnabled,
  onMoveTo,
}: {
  listing: DirListing;
  dir: string;
  communityId: string;
  serverId: string;
  canEdit: boolean;
  running: boolean;
  openFile: string | null;
  onEnter: (entry: DirEntry) => void;
  onChanged: () => void;
  onError: (error: unknown) => void;
  selected: Set<string>;
  onSelectionChange: (next: Set<string>) => void;
  lastClickedIdx: React.MutableRefObject<number | null>;
  dropEnabled: boolean;
  onMoveTo: (paths: string[], destDir: string) => Promise<void>;
}) {
  const { showToast } = useToast();
  const [renaming, setRenaming] = useState<DirEntry | null>(null);
  const [deleting, setDeleting] = useState<DirEntry | null>(null);
  const [contextMenu, setContextMenu] = useState<{
    entry: DirEntry;
    x: number;
    y: number;
  } | null>(null);

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

  // Drop-target state: which folder name is currently highlighted.
  const [folderDropTarget, setFolderDropTarget] = useState<string | null>(null);

  if (listing.entries.length === 0) {
    return <p className="sub">{t("files.empty")}</p>;
  }

  const handleDragStart = (e: React.DragEvent, full: string) => {
    if (!dropEnabled) {
      e.preventDefault();
      return;
    }
    // If the dragged item is part of the selection, move all selected items.
    // Otherwise move just the dragged item.
    const paths = selected.has(full) ? Array.from(selected) : [full];
    e.dataTransfer.setData("application/x-file-move", JSON.stringify(paths));
    e.dataTransfer.effectAllowed = "move";
  };

  const handleFolderDragOver = (e: React.DragEvent, entryName: string) => {
    if (
      !dropEnabled ||
      !e.dataTransfer.types.includes("application/x-file-move")
    )
      return;
    e.preventDefault();
    e.stopPropagation();
    setFolderDropTarget(entryName);
  };

  const handleFolderDragLeave = () => setFolderDropTarget(null);

  const handleFolderDrop = (e: React.DragEvent, entry: DirEntry) => {
    e.preventDefault();
    e.stopPropagation();
    setFolderDropTarget(null);
    if (!dropEnabled) return;
    const raw = e.dataTransfer.getData("application/x-file-move");
    if (!raw) return;
    const paths = JSON.parse(raw) as string[];
    const destDir = joinPath(dir, entry.name);
    void onMoveTo(paths, destDir);
  };

  return (
    <>
      {listing.truncated && (
        <div className="notice warn">{t("files.truncated")}</div>
      )}
      <ul className="file-list">
        {listing.entries.map((entry, idx) => {
          const full = joinPath(dir, entry.name);
          const isDropTarget = entry.is_dir && folderDropTarget === entry.name;
          return (
            <li
              key={entry.name}
              className={`file-row${openFile === full ? " active" : ""}${selected.has(full) ? " selected" : ""}${isDropTarget ? " drop-target" : ""}`}
              draggable={dropEnabled}
              onDragStart={(e) => handleDragStart(e, full)}
              onContextMenu={(e) => {
                e.preventDefault();
                setContextMenu({ entry, x: e.clientX, y: e.clientY });
              }}
              {...(entry.is_dir
                ? {
                    onDragOver: (e: React.DragEvent) =>
                      handleFolderDragOver(e, entry.name),
                    onDragLeave: handleFolderDragLeave,
                    onDrop: (e: React.DragEvent) => handleFolderDrop(e, entry),
                  }
                : {})}
            >
              {canEdit && (
                <input
                  type="checkbox"
                  className="file-select"
                  aria-label={entry.name}
                  checked={selected.has(full)}
                  onChange={() => {
                    /* handled by onClick for modifier-key support */
                  }}
                  onClick={(e) => {
                    if (e.shiftKey && lastClickedIdx.current !== null) {
                      const lo = Math.min(lastClickedIdx.current, idx);
                      const hi = Math.max(lastClickedIdx.current, idx);
                      const next = new Set(selected);
                      for (let i = lo; i <= hi; i++) {
                        next.add(joinPath(dir, listing.entries[i].name));
                      }
                      onSelectionChange(next);
                    } else if (e.ctrlKey || e.metaKey) {
                      const next = new Set(selected);
                      if (next.has(full)) {
                        next.delete(full);
                      } else {
                        next.add(full);
                      }
                      onSelectionChange(next);
                    } else {
                      const next = new Set(selected);
                      if (next.has(full)) {
                        next.delete(full);
                      } else {
                        next.add(full);
                      }
                      onSelectionChange(next);
                    }
                    lastClickedIdx.current = idx;
                  }}
                />
              )}
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
      {contextMenu !== null && (
        <FileContextMenu
          entry={contextMenu.entry}
          x={contextMenu.x}
          y={contextMenu.y}
          canEdit={canEdit}
          running={running}
          onClose={() => setContextMenu(null)}
          onOpen={() => {
            setContextMenu(null);
            onEnter(contextMenu.entry);
          }}
          onDownload={() => {
            setContextMenu(null);
            download.mutate(contextMenu.entry);
          }}
          onRename={() => {
            setContextMenu(null);
            setRenaming(contextMenu.entry);
          }}
          onDelete={() => {
            setContextMenu(null);
            setDeleting(contextMenu.entry);
          }}
        />
      )}
    </>
  );
}

// ── Context menu ────────────────────────────────────────────────────────────

function FileContextMenu({
  entry,
  x,
  y,
  canEdit,
  running,
  onClose,
  onOpen,
  onDownload,
  onRename,
  onDelete,
}: {
  entry: DirEntry;
  x: number;
  y: number;
  canEdit: boolean;
  running: boolean;
  onClose: () => void;
  onOpen: () => void;
  onDownload: () => void;
  onRename: () => void;
  onDelete: () => void;
}) {
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("mousedown", handleClick);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleKey);
    };
  }, [onClose]);

  return (
    <div
      ref={menuRef}
      className="file-context-menu"
      style={{ top: y, left: x }}
      role="menu"
    >
      <button type="button" role="menuitem" onClick={onOpen}>
        {t("files.contextMenu.open")}
      </button>
      <button type="button" role="menuitem" onClick={onDownload}>
        {entry.is_dir
          ? t("files.contextMenu.downloadZip")
          : t("files.contextMenu.download")}
      </button>
      {canEdit && !running && (
        <button type="button" role="menuitem" onClick={onRename}>
          {t("files.contextMenu.rename")}
        </button>
      )}
      {canEdit && !running && (
        <button
          type="button"
          role="menuitem"
          className="danger"
          onClick={onDelete}
        >
          {t("files.contextMenu.delete")}
        </button>
      )}
    </div>
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

// ── Toolbar: upload + mkdir + bulk operations ───────────────────────────────

function Toolbar({
  dir,
  communityId,
  serverId,
  canEdit,
  running,
  extract,
  setExtract,
  upload,
  onChanged,
  onError,
  selected,
  totalCount,
  onSelectAll,
  onDeselectAll,
  onClearSelection,
}: {
  dir: string;
  communityId: string;
  serverId: string;
  canEdit: boolean;
  running: boolean;
  extract: boolean;
  setExtract: (v: boolean) => void;
  upload: { mutate: (file: File) => void };
  onChanged: () => void;
  onError: (error: unknown) => void;
  selected: Set<string>;
  totalCount: number;
  onSelectAll: () => void;
  onDeselectAll: () => void;
  onClearSelection: () => void;
}) {
  const MAX_UPLOAD_BYTES = 512 * 1024 * 1024;
  const { showToast } = useToast();
  const [mkdirOpen, setMkdirOpen] = useState(false);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkMoveOpen, setBulkMoveOpen] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);

  const bulkDelete = async () => {
    const paths = Array.from(selected);
    const total = paths.length;
    setBulkDeleteOpen(false);
    setBulkBusy(true);
    let done = 0;
    let failed = 0;
    for (const path of paths) {
      showToast(t("files.bulk.delete.progress", { done, total }), "success");
      try {
        await api.delete(
          `${filesBase(communityId, serverId)}?path=${encodeURIComponent(path)}` as never,
        );
        done += 1;
      } catch (error) {
        failed += 1;
        if (onForbiddenCheck(error)) break;
      }
    }
    setBulkBusy(false);
    if (failed === 0) {
      showToast(t("files.bulk.delete.done", { done }), "success");
    } else {
      showToast(
        t("files.bulk.delete.partial", { done, total, failed }),
        "error",
      );
    }
    onClearSelection();
    onChanged();
  };

  const bulkDownload = async () => {
    const paths = Array.from(selected);
    const total = paths.length;
    setBulkBusy(true);
    let done = 0;
    let failed = 0;
    for (const path of paths) {
      showToast(t("files.bulk.download.progress", { done, total }), "success");
      const filename = path.split("/").at(-1) ?? path;
      try {
        await downloadFile(
          `${apiPath(
            "/api/communities/{community_id}/servers/{server_id}/files/download",
            { community_id: communityId, server_id: serverId },
          )}?path=${encodeURIComponent(path)}`,
          filename,
        );
        done += 1;
      } catch (error) {
        failed += 1;
        if (onForbiddenCheck(error)) break;
      }
    }
    setBulkBusy(false);
    if (failed === 0) {
      showToast(t("files.bulk.download.done", { done }), "success");
    } else {
      showToast(
        t("files.bulk.download.partial", { done, total, failed }),
        "error",
      );
    }
  };

  const bulkMove = async (dest: string) => {
    const paths = Array.from(selected);
    const total = paths.length;
    setBulkMoveOpen(false);
    setBulkBusy(true);
    let done = 0;
    let failed = 0;
    for (const path of paths) {
      showToast(t("files.bulk.move.progress", { done, total }), "success");
      const name = path.split("/").at(-1) ?? path;
      const to = dest === "" ? name : `${dest}/${name}`;
      try {
        await api.post(
          apiPath(
            "/api/communities/{community_id}/servers/{server_id}/files/rename",
            { community_id: communityId, server_id: serverId },
          ),
          { body: JSON.stringify({ from: path, to }) },
        );
        done += 1;
      } catch (error) {
        failed += 1;
        if (onForbiddenCheck(error)) break;
      }
    }
    setBulkBusy(false);
    if (failed === 0) {
      showToast(t("files.bulk.move.done", { done }), "success");
    } else {
      showToast(t("files.bulk.move.partial", { done, total, failed }), "error");
    }
    onClearSelection();
    onChanged();
  };

  /** Check if error is a 403 and route through onForbidden; returns true if so. */
  const onForbiddenCheck = (error: unknown): boolean => {
    if (error instanceof ApiError && error.status === 403) {
      onError(error);
      return true;
    }
    return false;
  };

  if (!canEdit) {
    return null;
  }

  const atRestTooltip = running
    ? t("files.error.serverMustBeStopped")
    : undefined;

  return (
    <>
      <div className="toolbar-row files-toolbar">
        {totalCount > 0 && (
          <>
            <button
              type="button"
              className="btn sm ghost"
              onClick={
                selected.size === totalCount ? onDeselectAll : onSelectAll
              }
            >
              {selected.size === totalCount
                ? t("files.deselectAll")
                : t("files.selectAll")}
            </button>
            {selected.size > 0 && (
              <span className="files-selected-count">
                {t("files.selectedCount", { count: selected.size })}
              </span>
            )}
          </>
        )}
        {selected.size > 0 && (
          <>
            <button
              type="button"
              className="btn sm danger"
              disabled={running || bulkBusy}
              title={atRestTooltip}
              onClick={() => setBulkDeleteOpen(true)}
            >
              {t("files.bulk.delete")}
            </button>
            <button
              type="button"
              className="btn sm"
              disabled={bulkBusy}
              onClick={() => void bulkDownload()}
            >
              {t("files.bulk.download")}
            </button>
            <button
              type="button"
              className="btn sm"
              disabled={running || bulkBusy}
              title={atRestTooltip}
              onClick={() => setBulkMoveOpen(true)}
            >
              {t("files.bulk.move")}
            </button>
          </>
        )}
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
      </div>
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
      <SimpleConfirmDialog
        open={bulkDeleteOpen}
        title={t("files.bulk.delete.dialogTitle")}
        body={t("files.bulk.delete.dialogBody", { count: selected.size })}
        confirmLabel={t("files.bulk.delete.confirm")}
        onConfirm={() => void bulkDelete()}
        onClose={() => setBulkDeleteOpen(false)}
      />
      {bulkMoveOpen && (
        <BulkMoveDialog
          onClose={() => setBulkMoveOpen(false)}
          onMove={(dest) => void bulkMove(dest)}
        />
      )}
    </>
  );
}

// ── Bulk move dialog ────────────────────────────────────────────────────────

function BulkMoveDialog({
  onClose,
  onMove,
}: {
  onClose: () => void;
  onMove: (dest: string) => void;
}) {
  const [dest, setDest] = useState("");

  return (
    <PromptDialog
      title={t("files.bulk.move.dialogTitle")}
      label={t("files.bulk.move.destLabel")}
      value={dest}
      onChange={setDest}
      confirmLabel={t("files.bulk.move.confirm")}
      onConfirm={() => onMove(dest.trim())}
      onClose={onClose}
    />
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
