import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useState } from "react";
import {
  ADMIN_COMMUNITIES_KEY,
  adminCommunitiesListKey,
} from "../api/adminQueryKeys.ts";
import { ApiError, api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { ConfirmDialog } from "../components/ConfirmDialog.tsx";
import { Modal } from "../components/Modal.tsx";
import { ResizableTable } from "../components/ResizableColumns.tsx";
import { useToast } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import { useOffsetParam } from "./urlState.ts";

// Platform admin Communities page (WEBUI_SPEC.md 6.12): list ALL communities and
// provision new ones (name + initial owner). The listing reads the platform-axis
// endpoint GET /admin/communities (#489) — admin sees every community with its
// member/server counts, regardless of membership — paginated like
// GET /admin/users.
// Provision (and delete) invalidate both this admin list and the membership-
// scoped ["communities"] query the community switcher reuses.

type AdminCommunityResponse = components["schemas"]["AdminCommunityResponse"];
type AdminUserResponse = components["schemas"]["AdminUserResponse"];

// The API caps a single page at 100 and defaults to 50 (admin_communities.py).
const PAGE_SIZE = 50;

function communitiesUrl(offset: number): "/api/admin/communities" {
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(offset),
  });
  return `/api/admin/communities?${params.toString()}` as "/api/admin/communities";
}

export function AdminCommunitiesPage() {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [provisionOpen, setProvisionOpen] = useState(false);
  // Page offset lives in `?offset=N` (#514) so Back restores the prior page.
  const [offset, setOffset] = useOffsetParam();
  const [deleteTarget, setDeleteTarget] =
    useState<AdminCommunityResponse | null>(null);

  const communities = useQuery({
    queryKey: adminCommunitiesListKey(PAGE_SIZE, offset),
    queryFn: ({ signal }) => api.get(communitiesUrl(offset), { signal }),
    placeholderData: keepPreviousData,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ADMIN_COMMUNITIES_KEY });
    // The membership-scoped switcher list shares this key.
    queryClient.invalidateQueries({ queryKey: ["communities"] });
  };

  const remove = useMutation({
    mutationFn: (id: string) =>
      api.delete(
        apiPath("/api/communities/{community_id}", { community_id: id }),
      ),
    onSuccess: () => {
      showToast(t("admin.communities.deleted"), "success");
      invalidate();
    },
    onError: (err) => {
      const reason = err instanceof ApiError ? err.reason : undefined;
      showToast(
        reason === "not_found"
          ? t("admin.communities.deleted")
          : t("admin.communities.deleteError"),
        reason === "not_found" ? "success" : "error",
      );
      if (reason === "not_found") {
        invalidate();
      }
    },
  });

  const onConfirmDelete = () => {
    if (deleteTarget === null) {
      return;
    }
    const target = deleteTarget;
    setDeleteTarget(null);
    remove.mutate(target.id);
  };

  const total = communities.data?.total ?? 0;
  const rows = communities.data?.communities ?? [];
  const hasNext = offset + PAGE_SIZE < total;
  const rangeFrom = total === 0 ? 0 : offset + 1;
  const rangeTo = offset + rows.length;

  return (
    <div className="admin-communities">
      <div className="page-head">
        <div>
          <h1>{t("page.adminCommunities")}</h1>
          <div className="sub">{t("admin.communities.subtitle")}</div>
        </div>
        <div className="actions">
          <button
            type="button"
            className="btn primary"
            onClick={() => setProvisionOpen(true)}
          >
            {t("admin.communities.provision")}
          </button>
        </div>
      </div>

      {communities.isPending ? (
        <p className="sub" role="status">
          {t("admin.communities.loading")}
        </p>
      ) : communities.isError ? (
        <p className="field-error" role="alert">
          {t("admin.communities.loadError")}
        </p>
      ) : (
        <CommunityTable
          communities={rows}
          pending={remove.isPending}
          onDelete={setDeleteTarget}
        />
      )}

      <div className="users-paging">
        <button
          type="button"
          className="btn sm ghost"
          disabled={offset === 0 || communities.isFetching}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        >
          {t("admin.communities.prev")}
        </button>
        <span className="sub">
          {t("admin.communities.range", {
            from: rangeFrom,
            to: rangeTo,
            total,
          })}
        </span>
        <button
          type="button"
          className="btn sm ghost"
          disabled={!hasNext || communities.isFetching}
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          {t("admin.communities.next")}
        </button>
      </div>

      <ProvisionDialog
        open={provisionOpen}
        onClose={() => setProvisionOpen(false)}
        onProvisioned={() => {
          setProvisionOpen(false);
          showToast(t("admin.communities.provisioned"), "success");
          invalidate();
        }}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        title={t("admin.communities.deleteTitle")}
        body={t("admin.communities.deleteBody")}
        confirmPhrase={deleteTarget?.name ?? ""}
        confirmLabel={t("admin.communities.deleteConfirm")}
        promptLabel={t("admin.communities.deletePrompt")}
        onConfirm={onConfirmDelete}
        onClose={() => setDeleteTarget(null)}
      />
    </div>
  );
}

function CommunityTable({
  communities,
  pending,
  onDelete,
}: {
  communities: AdminCommunityResponse[];
  pending: boolean;
  onDelete: (community: AdminCommunityResponse) => void;
}) {
  if (communities.length === 0) {
    return <p className="sub">{t("admin.communities.empty")}</p>;
  }
  return (
    <div className="card" style={{ padding: 0 }}>
      <ResizableTable storageKey="mcsd.colw.admin-communities" className="data">
        <thead>
          <tr>
            <th>{t("admin.communities.colName")}</th>
            <th>{t("admin.communities.colId")}</th>
            <th className="num">{t("admin.communities.colMembers")}</th>
            <th className="num">{t("admin.communities.colServers")}</th>
            <th>{t("admin.communities.colActions")}</th>
          </tr>
        </thead>
        <tbody>
          {communities.map((c) => (
            <tr key={c.id}>
              <td>
                <strong>{c.name}</strong>
              </td>
              <td className="mono" title={c.id}>
                {c.id}
              </td>
              <td className="num">{c.member_count}</td>
              <td className="num">{c.server_count}</td>
              <td className="row-actions">
                <button
                  type="button"
                  className="btn sm danger"
                  disabled={pending}
                  onClick={() => onDelete(c)}
                >
                  {t("admin.communities.delete")}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </ResizableTable>
    </div>
  );
}

// Map a provision rejection to a specific inline message. The provision
// endpoint returns 409 name_taken, 422 invalid_name, 422 owner_not_found and
// 422 invalid_owner_user_id (communities.py). The owner picker only ever sends a
// known id, so invalid_owner_user_id falls through to the generic message.
function provisionErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.reason === "name_taken") {
      return t("admin.communities.errNameTaken");
    }
    if (error.reason === "invalid_name") {
      return t("admin.communities.errInvalidName");
    }
    if (error.reason === "owner_not_found") {
      return t("admin.communities.errOwnerNotFound");
    }
  }
  return t("admin.communities.errGeneric");
}

function ProvisionDialog({
  open,
  onClose,
  onProvisioned,
}: {
  open: boolean;
  onClose: () => void;
  onProvisioned: () => void;
}) {
  const [name, setName] = useState("");
  const [ownerId, setOwnerId] = useState("");
  const [error, setError] = useState<string | null>(null);

  // Owner picker source: GET /admin/users is the only listing endpoint
  // (admin-only, paginated; max limit=100, default 50 — admin_users.py). The
  // provision API takes a user UUID, so the picker shows username/email and
  // submits the selected user's id. Request the max page so deployments with
  // >50 accounts are not silently truncated; when total exceeds it we surface a
  // hint below. Only fetched while open.
  const users = useQuery({
    queryKey: ["users"],
    queryFn: ({ signal }) =>
      api.get("/api/admin/users?limit=100&offset=0" as "/api/admin/users", {
        signal,
      }),
    enabled: open,
  });

  const close = () => {
    setName("");
    setOwnerId("");
    setError(null);
    onClose();
  };

  const provision = useMutation({
    mutationFn: (body: { name: string; owner_user_id: string }) =>
      api.post("/api/communities", { body: JSON.stringify(body) }),
    onSuccess: () => {
      setName("");
      setOwnerId("");
      setError(null);
      onProvisioned();
    },
    onError: (err) => {
      setError(provisionErrorMessage(err));
    },
  });

  const submit = () => {
    const trimmed = name.trim();
    if (trimmed.length === 0) {
      setError(t("admin.communities.errNameRequired"));
      return;
    }
    if (ownerId.length === 0) {
      setError(t("admin.communities.errOwnerRequired"));
      return;
    }
    setError(null);
    provision.mutate({ name: trimmed, owner_user_id: ownerId });
  };

  const userList: AdminUserResponse[] = users.data?.users ?? [];
  // The API caps the page at 100; if more accounts exist, the owner picker is
  // incomplete — say so rather than silently omitting the later users (#476).
  const userTotal = users.data?.total ?? 0;
  const usersTruncated = userTotal > userList.length;

  return (
    <Modal
      open={open}
      title={t("admin.communities.dialogTitle")}
      onClose={close}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={close}>
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={provision.isPending}
            onClick={submit}
          >
            {t("admin.communities.provisionSubmit")}
          </button>
        </>
      }
    >
      <label className="field">
        {t("admin.communities.nameLabel")}
        <input
          type="text"
          value={name}
          placeholder={t("admin.communities.namePlaceholder")}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="field">
        {t("admin.communities.ownerLabel")}
        <select
          value={ownerId}
          disabled={users.isPending || users.isError}
          onChange={(e) => setOwnerId(e.target.value)}
        >
          <option value="">{t("admin.communities.ownerPlaceholder")}</option>
          {userList.map((u) => (
            <option key={u.id} value={u.id}>
              {u.username} ({u.email})
            </option>
          ))}
        </select>
      </label>
      <div className="hint">{t("admin.communities.ownerHint")}</div>
      {usersTruncated && (
        <div className="hint">
          {t("admin.communities.usersTruncated", {
            n: userList.length,
            total: userTotal,
          })}
        </div>
      )}
      {users.isError && (
        <span className="field-error">
          {t("admin.communities.usersLoadError")}
        </span>
      )}
      {error !== null && <span className="field-error">{error}</span>}
    </Modal>
  );
}
