import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client.ts";
import type { components } from "../api/schema";
import { Modal } from "../components/Modal.tsx";
import { useToast } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";

// Platform admin Communities page (WEBUI_SPEC.md 6.12): list communities and
// provision new ones (name + initial owner). The list reads ["communities"] —
// the same key the community switcher uses — so a successful provision
// invalidates both views at once. CommunityResponse carries only {id, name};
// the API exposes no admin-wide community list, so this shows the communities
// the admin is a member of (#476).

type CommunityResponse = components["schemas"]["CommunityResponse"];
type AdminUserResponse = components["schemas"]["AdminUserResponse"];

export function AdminCommunitiesPage() {
  const [provisionOpen, setProvisionOpen] = useState(false);

  const communities = useQuery({
    queryKey: ["communities"],
    queryFn: () => api.get("/communities"),
  });

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
      ) : communities.isError || communities.data === undefined ? (
        <p className="field-error" role="alert">
          {t("admin.communities.loadError")}
        </p>
      ) : (
        <CommunityTable communities={communities.data} />
      )}

      <ProvisionDialog
        open={provisionOpen}
        onClose={() => setProvisionOpen(false)}
      />
    </div>
  );
}

function CommunityTable({ communities }: { communities: CommunityResponse[] }) {
  if (communities.length === 0) {
    return <p className="sub">{t("admin.communities.empty")}</p>;
  }
  return (
    <div className="card" style={{ padding: 0 }}>
      <table className="data">
        <thead>
          <tr>
            <th>{t("admin.communities.colName")}</th>
            <th>{t("admin.communities.colId")}</th>
          </tr>
        </thead>
        <tbody>
          {communities.map((c) => (
            <tr key={c.id}>
              <td>
                <strong>{c.name}</strong>
              </td>
              <td className="mono">{c.id}</td>
            </tr>
          ))}
        </tbody>
      </table>
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
}: {
  open: boolean;
  onClose: () => void;
}) {
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [ownerId, setOwnerId] = useState("");
  const [error, setError] = useState<string | null>(null);

  // Owner picker source: GET /users is the only listing endpoint (admin-only,
  // paginated; max limit=100, default 50 — admin_users.py). The provision API
  // takes a user UUID, so the picker shows username/email and submits the
  // selected user's id. Request the max page so deployments with >50 accounts
  // are not silently truncated; when total exceeds it we surface a hint below.
  // Only fetched while open.
  const users = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get("/users?limit=100&offset=0" as "/users"),
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
      api.post("/communities", { body: JSON.stringify(body) }),
    onSuccess: () => {
      showToast(t("admin.communities.provisioned"), "success");
      queryClient.invalidateQueries({ queryKey: ["communities"] });
      close();
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
          {t("admin.communities.usersTruncatedPrefix")}
          {userList.length}
          {t("admin.communities.usersTruncatedMid")}
          {userTotal}
          {t("admin.communities.usersTruncatedSuffix")}
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
