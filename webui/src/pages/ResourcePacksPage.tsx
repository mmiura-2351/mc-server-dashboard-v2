/**
 * Resource pack library management page (issue #1178).
 *
 * Lists all resource packs, with upload (multipart), download (authenticated
 * blob), and delete (typed-confirm) actions. Available to all authenticated
 * users; delete is visible only to the uploader or platform admins.
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
import { formatDateTime, humanizeBytes, shortId } from "../format.ts";
import { t } from "../i18n/index.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";

type ResourcePackResponse = components["schemas"]["ResourcePackResponse"];

const PACKS_KEY = ["resource-packs"] as const;

export function ResourcePacksPage() {
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const onForbidden = useOnForbidden();
  const currentUser = useCurrentUser();
  const isAdmin = currentUser.data?.is_platform_admin === true;
  const userId = currentUser.data?.id;

  const [uploadOpen, setUploadOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ResourcePackResponse | null>(
    null,
  );

  const listQuery = useQuery({
    queryKey: PACKS_KEY,
    queryFn: () => api.get("/api/resource-packs"),
  });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: PACKS_KEY });
  };

  const remove = useMutation({
    mutationFn: (pack: ResourcePackResponse) =>
      api.delete(
        apiPath("/api/resource-packs/{resource_pack_id}", {
          resource_pack_id: pack.id,
        }),
      ),
    onSuccess: () => {
      showToast(t("resourcePacks.deleted"), "success");
      refresh();
    },
    onError: (error) => {
      if (onForbidden(error)) return;
      if (
        error instanceof ApiError &&
        error.reason === "resource_pack_in_use"
      ) {
        showToast(t("resourcePacks.error.inUse"), "error");
      } else {
        showToast(t("resourcePacks.error.deleteFailed"), "error");
      }
    },
  });

  const download = useMutation({
    mutationFn: (pack: ResourcePackResponse) =>
      downloadFile(
        apiPath("/api/resource-packs/{resource_pack_id}/download", {
          resource_pack_id: pack.id,
        }),
        pack.filename,
      ),
    onError: (error) => {
      if (onForbidden(error)) return;
      showToast(t("resourcePacks.error.downloadFailed"), "error");
    },
  });

  function canDelete(pack: ResourcePackResponse): boolean {
    return isAdmin || pack.uploaded_by === userId;
  }

  return (
    <div className="resource-packs">
      <div className="page-head">
        <div>
          <h1>{t("page.resourcePacks")}</h1>
          <div className="sub">{t("resourcePacks.subtitle")}</div>
        </div>
        <div className="actions">
          <button
            type="button"
            className="btn primary"
            onClick={() => setUploadOpen(true)}
          >
            {t("resourcePacks.upload")}
          </button>
        </div>
      </div>

      {listQuery.isPending ? (
        <p className="sub" role="status">
          {t("resourcePacks.loading")}
        </p>
      ) : listQuery.isError ? (
        <p className="field-error" role="alert">
          {t("resourcePacks.loadError")}
        </p>
      ) : (
        <div className="card table-card">
          {listQuery.data.resource_packs.length === 0 ? (
            <p className="sub">{t("resourcePacks.empty")}</p>
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>{t("resourcePacks.col.displayName")}</th>
                  <th>{t("resourcePacks.col.filename")}</th>
                  <th>{t("resourcePacks.col.size")}</th>
                  <th>{t("resourcePacks.col.sha1")}</th>
                  <th>{t("resourcePacks.col.uploaded")}</th>
                  <th>{t("resourcePacks.col.uploader")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {listQuery.data.resource_packs.map((pack) => (
                  <tr key={pack.id}>
                    <td>{pack.display_name}</td>
                    <td>{pack.filename}</td>
                    <td className="num">{humanizeBytes(pack.size_bytes)}</td>
                    <td title={pack.sha1_hash}>{pack.sha1_hash.slice(0, 8)}</td>
                    <td>{formatDateTime(pack.created_at)}</td>
                    <td title={pack.uploaded_by}>
                      {shortId(pack.uploaded_by)}
                    </td>
                    <td className="row-actions">
                      <button
                        type="button"
                        className="btn sm"
                        onClick={() => download.mutate(pack)}
                      >
                        {t("resourcePacks.download")}
                      </button>
                      {canDelete(pack) && (
                        <button
                          type="button"
                          className="btn sm danger"
                          onClick={() => setDeleteTarget(pack)}
                        >
                          {t("resourcePacks.delete")}
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
            showToast(t("resourcePacks.uploaded"), "success");
            refresh();
          }}
          onClose={() => setUploadOpen(false)}
        />
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title={t("resourcePacks.deleteDialog.title")}
        body={t("resourcePacks.deleteDialog.body")}
        confirmPhrase={deleteTarget?.display_name ?? ""}
        confirmLabel={t("resourcePacks.deleteDialog.confirm")}
        promptLabel={t("resourcePacks.deleteDialog.prompt")}
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
  const [file, setFile] = useState<File | null>(null);

  const upload = useMutation({
    mutationFn: ({ name, f }: { name: string; f: File }) => {
      const form = new FormData();
      form.append("display_name", name);
      form.append("file", f);
      return api.postForm("/api/resource-packs", form);
    },
    onSuccess,
    onError: (error) => {
      if (onForbidden(error)) return;
      showToast(t("resourcePacks.error.uploadFailed"), "error");
    },
  });

  const nameEmpty = displayName.trim() === "";

  return (
    <Modal
      open={true}
      title={t("resourcePacks.uploadDialog.title")}
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
                  showToast(t("resourcePacks.error.tooLarge"), "error");
                  return;
                }
                upload.mutate({ name: displayName.trim(), f: file });
              }
            }}
          >
            {upload.isPending
              ? t("resourcePacks.uploadDialog.uploading")
              : t("resourcePacks.uploadDialog.submit")}
          </button>
        </>
      }
    >
      <label className="field">
        {t("resourcePacks.uploadDialog.displayName")}
        <input
          type="text"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
        />
      </label>
      <label className="field" htmlFor="rp-upload-file">
        {t("resourcePacks.uploadDialog.file")}
      </label>
      <FilePicker
        id="rp-upload-file"
        accept=".zip"
        file={file}
        onSelect={setFile}
      />
    </Modal>
  );
}
