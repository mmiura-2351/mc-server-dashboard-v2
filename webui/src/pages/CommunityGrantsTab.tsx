import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client.ts";
import { membersKeys } from "../api/communityQueryKeys.ts";
import { labelQueryFn } from "../api/labelQuery.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { Modal } from "../components/Modal.tsx";
import { SimpleConfirmDialog } from "../components/SimpleConfirmDialog.tsx";
import { useToast } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import {
  COMMUNITY_PERMISSION_CODES,
  type CommunityPermissionCode,
} from "../permissions/catalog.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";

type GrantResponse = components["schemas"]["GrantResponse"];
type MemberResponse = components["schemas"]["MemberResponse"];
type ServerResponse = components["schemas"]["ServerResponse"];

// Grants are per-resource and resource_type is always `server` (WEBUI_SPEC.md
// 2.2), so the permission picker is restricted to the server/file/backup
// families — derived from the typed catalog so it can never drift.
const GRANTABLE_FAMILIES = ["server", "file", "backup"] as const;
const GRANTABLE_CODES: readonly CommunityPermissionCode[] =
  COMMUNITY_PERMISSION_CODES.filter((code) =>
    (GRANTABLE_FAMILIES as readonly string[]).includes(code.split(":")[0]),
  );

function grantsKey(communityId: string, userId: string) {
  return ["communities", communityId, "grants", userId] as const;
}

// Grants tab (WEBUI_SPEC.md 6.10): list per-server permission grants with a
// member filter, create a grant (member -> server -> codes), revoke with a
// typed confirm. List is gated by `grant:read`, create/revoke by `grant:manage`
// (the API gates them there); 403s route through onForbidden.
export function CommunityGrantsTab({
  communityId,
  can,
}: {
  communityId: string;
  can: Can;
}) {
  const canManage = can("grant:manage");
  const [filterUserId, setFilterUserId] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [revoking, setRevoking] = useState<GrantResponse | null>(null);

  // Members and servers label the grant rows (the grant payload carries only
  // ids) and feed the create-flow pickers. These are display-only secondary
  // reads under different gates (`member:read` / `server:read`) than the tab's
  // `grant:read` primary read, so a 403 degrades to an empty list (raw-id
  // fallback) via `labelQueryFn` instead of failing the whole tab (#471).
  const members = useQuery({
    queryKey: membersKeys.list(communityId),
    queryFn: labelQueryFn(
      ({ signal }: { signal: AbortSignal }) =>
        api.get(
          apiPath("/api/communities/{community_id}/members", {
            community_id: communityId,
          }),
          { signal },
        ),
      [],
    ),
  });
  const servers = useQuery({
    // Use a "grants-labels" suffix so a 403→[] fallback is isolated to this
    // query and does not pollute the shared ["communities", cid, "servers"] key
    // that DashboardPage and CommunityGroupsTab consume (#791).
    queryKey: ["communities", communityId, "servers", "grants-labels"] as const,
    queryFn: labelQueryFn(
      ({ signal }: { signal: AbortSignal }) =>
        api.get(
          apiPath("/api/communities/{community_id}/servers", {
            community_id: communityId,
          }),
          { signal },
        ),
      [],
    ),
  });

  const grants = useQuery({
    queryKey: grantsKey(communityId, filterUserId),
    queryFn: ({ signal }) => {
      const base = apiPath("/api/communities/{community_id}/grants", {
        community_id: communityId,
      });
      // `?user_id=` is a schema query param, not part of the path key; append it
      // to the interpolated path while keeping the path-literal type.
      const url =
        filterUserId === ""
          ? base
          : (`${base}?user_id=${encodeURIComponent(filterUserId)}` as typeof base);
      return api.get(url, { signal });
    },
  });

  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();

  const revoke = useMutation({
    mutationFn: (grant: GrantResponse) =>
      api.delete(
        apiPath("/api/communities/{community_id}/grants/{grant_id}", {
          community_id: communityId,
          grant_id: grant.id,
        }),
      ),
    onSuccess: () => {
      showToast(t("communitySettings.grants.revoked"), "success");
      queryClient.invalidateQueries({
        queryKey: ["communities", communityId, "grants"],
      });
      setRevoking(null);
    },
    onError: (error) => {
      setRevoking(null);
      if (onForbidden(error)) {
        return;
      }
      showToast(t("communitySettings.grants.revokeError"), "error");
    },
  });

  if (grants.isPending || members.isPending || servers.isPending) {
    return <p className="sub">{t("communitySettings.grants.loading")}</p>;
  }
  // Error only when there is nothing to show (an initial load failed). A
  // failed background refetch retains `data`, so the cached list keeps
  // rendering through transient API blips (#1797).
  if (
    grants.data === undefined ||
    members.data === undefined ||
    servers.data === undefined
  ) {
    return (
      <p className="field-error">{t("communitySettings.grants.loadError")}</p>
    );
  }

  const usernameById = new Map(
    members.data.map((m) => [m.user_id, m.username]),
  );
  const serverNameById = new Map(servers.data.map((s) => [s.id, s.name]));

  return (
    <section className="grants">
      <div className="page-head">
        <h2>{t("communitySettings.grants.heading")}</h2>
        {canManage && (
          <button
            type="button"
            className="btn primary"
            onClick={() => setCreateOpen(true)}
          >
            {t("communitySettings.grants.create")}
          </button>
        )}
      </div>

      <label className="field grants-filter">
        {t("communitySettings.grants.filterLabel")}
        <select
          value={filterUserId}
          onChange={(e) => setFilterUserId(e.target.value)}
        >
          <option value="">{t("communitySettings.grants.filterAll")}</option>
          {members.data.map((m) => (
            <option key={m.user_id} value={m.user_id}>
              {m.username ?? t("communitySettings.grants.unknownUser")}
            </option>
          ))}
        </select>
      </label>

      {grants.data.length === 0 ? (
        <p className="sub">{t("communitySettings.grants.empty")}</p>
      ) : (
        <table className="data">
          <thead>
            <tr>
              <th>{t("communitySettings.grants.colMember")}</th>
              <th>{t("communitySettings.grants.colServer")}</th>
              <th>{t("communitySettings.grants.colPermissions")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {grants.data.map((grant) => (
              <tr key={grant.id}>
                <td>{usernameById.get(grant.user_id) ?? grant.user_id}</td>
                <td>
                  {serverNameById.get(grant.resource_id) ?? grant.resource_id}
                </td>
                <td>
                  <span className="chips">
                    {grant.permissions.map((code) => (
                      <span className="chip" key={code}>
                        {code}
                      </span>
                    ))}
                  </span>
                </td>
                <td>
                  {canManage && (
                    <button
                      type="button"
                      className="btn sm danger"
                      onClick={() => setRevoking(grant)}
                    >
                      {t("communitySettings.grants.revoke")}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {canManage && (
        <CreateGrantDialog
          open={createOpen}
          communityId={communityId}
          members={members.data}
          servers={servers.data}
          onClose={() => setCreateOpen(false)}
        />
      )}

      <SimpleConfirmDialog
        open={revoking !== null}
        title={t("communitySettings.grants.revokeDialogTitle")}
        body={t("communitySettings.grants.revokeDialogBody")}
        confirmLabel={t("communitySettings.grants.revokeConfirm")}
        busy={revoke.isPending}
        onConfirm={() => {
          if (revoking !== null) {
            revoke.mutate(revoking);
          }
        }}
        onClose={() => setRevoking(null)}
      />
    </section>
  );
}

function CreateGrantDialog({
  open,
  communityId,
  members,
  servers,
  onClose,
}: {
  open: boolean;
  communityId: string;
  members: MemberResponse[];
  servers: ServerResponse[];
  onClose: () => void;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const [userId, setUserId] = useState("");
  const [serverId, setServerId] = useState("");
  const [codes, setCodes] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  const close = () => {
    setUserId("");
    setServerId("");
    setCodes(new Set());
    setError(null);
    onClose();
  };

  const create = useMutation({
    mutationFn: () =>
      api.post(
        apiPath("/api/communities/{community_id}/grants", {
          community_id: communityId,
        }),
        {
          body: JSON.stringify({
            user_id: userId,
            resource_type: "server",
            resource_id: serverId,
            permissions: GRANTABLE_CODES.filter((c) => codes.has(c)),
          }),
        },
      ),
    onSuccess: () => {
      showToast(t("communitySettings.grants.created"), "success");
      queryClient.invalidateQueries({
        queryKey: ["communities", communityId, "grants"],
      });
      close();
    },
    onError: (err) => {
      if (onForbidden(err)) {
        close();
        return;
      }
      setError(t("communitySettings.grants.createError"));
    },
  });

  const toggle = (code: string) =>
    setCodes((prev) => {
      const next = new Set(prev);
      if (next.has(code)) {
        next.delete(code);
      } else {
        next.add(code);
      }
      return next;
    });

  const submit = () => {
    if (userId === "" || serverId === "" || codes.size === 0) {
      setError(t("communitySettings.grants.createIncomplete"));
      return;
    }
    setError(null);
    create.mutate();
  };

  return (
    <Modal
      open={open}
      title={t("communitySettings.grants.createDialogTitle")}
      onClose={close}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={close}>
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={create.isPending}
            onClick={submit}
          >
            {t("communitySettings.grants.createSubmit")}
          </button>
        </>
      }
    >
      <p>{t("communitySettings.grants.createDialogBody")}</p>
      <label className="field">
        {t("communitySettings.grants.memberLabel")}
        <select value={userId} onChange={(e) => setUserId(e.target.value)}>
          <option value="">
            {t("communitySettings.grants.memberPlaceholder")}
          </option>
          {members.map((m) => (
            <option key={m.user_id} value={m.user_id}>
              {m.username ?? t("communitySettings.grants.unknownUser")}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        {t("communitySettings.grants.serverLabel")}
        <select value={serverId} onChange={(e) => setServerId(e.target.value)}>
          <option value="">
            {t("communitySettings.grants.serverPlaceholder")}
          </option>
          {servers.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </label>
      <fieldset className="field grants-codes">
        <legend>{t("communitySettings.grants.permissionsLabel")}</legend>
        {GRANTABLE_CODES.map((code) => (
          <label className="checkbox" key={code}>
            <input
              type="checkbox"
              checked={codes.has(code)}
              onChange={() => toggle(code)}
            />
            {code}
          </label>
        ))}
      </fieldset>
      {error !== null && <span className="field-error">{error}</span>}
    </Modal>
  );
}
