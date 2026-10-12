import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client.ts";
import { membersKeys, rolesKeys } from "../api/communityQueryKeys.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { Modal } from "../components/Modal.tsx";
import { SimpleConfirmDialog } from "../components/SimpleConfirmDialog.tsx";
import { useToast } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { classifyQueryResult } from "../queryState.ts";

type MemberResponse = components["schemas"]["MemberResponse"];
type RoleResponse = components["schemas"]["RoleResponse"];

export function CommunityMembersTab({
  communityId,
  can,
}: {
  communityId: string;
  can: Can;
}) {
  const canAdd = can("member:add");
  const canRemove = can("member:remove");
  const canManageRoles = can("role:manage");
  const canReadRoles = can("role:read");

  const members = useQuery({
    queryKey: membersKeys.list(communityId),
    queryFn: ({ signal }) =>
      api.get(
        apiPath("/api/communities/{community_id}/members", {
          community_id: communityId,
        }),
        { signal },
      ),
  });

  const roles = useQuery({
    queryKey: rolesKeys.list(communityId),
    queryFn: ({ signal }) =>
      api.get(
        apiPath("/api/communities/{community_id}/roles", {
          community_id: communityId,
        }),
        { signal },
      ),
    enabled: canReadRoles && canManageRoles,
  });

  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);
  const [removing, setRemoving] = useState<MemberResponse | null>(null);

  const remove = useMutation({
    mutationFn: (member: MemberResponse) =>
      api.delete(
        apiPath("/api/communities/{community_id}/members/{user_id}", {
          community_id: communityId,
          user_id: member.user_id,
        }),
      ),
    onSuccess: () => {
      showToast(t("communitySettings.members.removed"), "success");
      queryClient.invalidateQueries({
        queryKey: membersKeys.list(communityId),
      });
      setRemoving(null);
    },
    onError: (error) => {
      setRemoving(null);
      if (onForbidden(error)) {
        return;
      }
      showToast(t("communitySettings.members.removeError"), "error");
    },
  });

  const membersState = classifyQueryResult(members);

  if (membersState.kind === "pending") {
    return <p className="sub">{t("communitySettings.members.loading")}</p>;
  }
  // Retain cached content when only a background refetch fails.
  if (membersState.kind === "error") {
    return (
      <p className="field-error">{t("communitySettings.members.loadError")}</p>
    );
  }

  const roleList = roles.data ?? [];
  return (
    <section>
      <div className="page-head">
        <h2>{t("communitySettings.members.heading")}</h2>
        {canAdd && (
          <button
            type="button"
            className="btn primary"
            onClick={() => setAddOpen(true)}
          >
            {t("communitySettings.members.add")}
          </button>
        )}
      </div>

      {membersState.data.length === 0 ? (
        <p className="sub">{t("communitySettings.members.empty")}</p>
      ) : (
        <table className="data">
          <thead>
            <tr>
              <th>{t("communitySettings.members.colUsername")}</th>
              <th>{t("communitySettings.members.colRoles")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {membersState.data.map((member) => (
              <MemberRow
                key={member.membership_id}
                communityId={communityId}
                member={member}
                roles={roleList}
                canManageRoles={canManageRoles && canReadRoles}
                canRemove={canRemove}
                onRemove={() => setRemoving(member)}
              />
            ))}
          </tbody>
        </table>
      )}

      {canAdd && (
        <AddMemberDialog
          open={addOpen}
          communityId={communityId}
          onClose={() => setAddOpen(false)}
        />
      )}

      <SimpleConfirmDialog
        open={removing !== null}
        title={t("communitySettings.members.removeDialogTitle")}
        body={t("communitySettings.members.removeDialogBody")}
        confirmLabel={t("communitySettings.members.removeConfirm")}
        busy={remove.isPending}
        onConfirm={() => {
          if (removing !== null) {
            remove.mutate(removing);
          }
        }}
        onClose={() => setRemoving(null)}
      />
    </section>
  );
}

function MemberRow({
  communityId,
  member,
  roles,
  canManageRoles,
  canRemove,
  onRemove,
}: {
  communityId: string;
  member: MemberResponse;
  roles: RoleResponse[];
  canManageRoles: boolean;
  canRemove: boolean;
  onRemove: () => void;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const [picking, setPicking] = useState(false);

  const onRoleError = (error: unknown) => {
    if (onForbidden(error)) {
      return;
    }
    showToast(t("communitySettings.members.roleError"), "error");
  };
  const onRoleSettled = () =>
    queryClient.invalidateQueries({ queryKey: membersKeys.list(communityId) });

  const assign = useMutation({
    mutationFn: (roleId: string) =>
      api.post(
        apiPath("/api/communities/{community_id}/members/{user_id}/roles", {
          community_id: communityId,
          user_id: member.user_id,
        }),
        { body: JSON.stringify({ role_id: roleId }) },
      ),
    onSettled: onRoleSettled,
    onError: onRoleError,
  });

  const unassign = useMutation({
    mutationFn: (roleId: string) =>
      api.delete(
        apiPath(
          "/api/communities/{community_id}/members/{user_id}/roles/{role_id}",
          {
            community_id: communityId,
            user_id: member.user_id,
            role_id: roleId,
          },
        ),
      ),
    onSettled: onRoleSettled,
    onError: onRoleError,
  });

  const roleByName = new Map(roles.map((r) => [r.name, r]));
  const held = new Set(member.role_names);
  const assignable = roles.filter((r) => !held.has(r.name));
  const busy = assign.isPending || unassign.isPending;

  return (
    <tr>
      <td>{member.username ?? t("communitySettings.members.unknownUser")}</td>
      <td>
        <span className="chips">
          {member.role_names.map((name) => {
            const role = roleByName.get(name);
            return (
              <span className="chip" key={name}>
                {name}
                {canManageRoles && role !== undefined && (
                  <button
                    type="button"
                    className="chip-x"
                    aria-label={`${t("communitySettings.members.unassignRole")}: ${name}`}
                    disabled={busy}
                    onClick={() => unassign.mutate(role.id)}
                  >
                    ×
                  </button>
                )}
              </span>
            );
          })}
          {canManageRoles && (
            <span className="chip-assign">
              <button
                type="button"
                className="btn sm ghost"
                aria-label={t("communitySettings.members.assignRole")}
                disabled={busy}
                onClick={() => setPicking((v) => !v)}
              >
                +
              </button>
              {picking && (
                <span className="chip-menu" role="menu">
                  {assignable.length === 0 ? (
                    <span className="sub">
                      {t("communitySettings.members.noRolesLeft")}
                    </span>
                  ) : (
                    assignable.map((role) => (
                      <button
                        key={role.id}
                        type="button"
                        role="menuitem"
                        className="btn sm"
                        disabled={busy}
                        onClick={() => {
                          setPicking(false);
                          assign.mutate(role.id);
                        }}
                      >
                        {role.name}
                      </button>
                    ))
                  )}
                </span>
              )}
            </span>
          )}
        </span>
      </td>
      <td>
        {canRemove && (
          <button type="button" className="btn sm danger" onClick={onRemove}>
            {t("communitySettings.members.remove")}
          </button>
        )}
      </td>
    </tr>
  );
}

function addErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.reason === "user_not_found") {
      return t("communitySettings.members.errUserNotFound");
    }
    if (error.reason === "already_member") {
      return t("communitySettings.members.errAlreadyMember");
    }
  }
  return t("communitySettings.members.errGeneric");
}

function AddMemberDialog({
  open,
  communityId,
  onClose,
}: {
  open: boolean;
  communityId: string;
  onClose: () => void;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const [username, setUsername] = useState("");
  const [error, setError] = useState<string | null>(null);

  const close = () => {
    setUsername("");
    setError(null);
    onClose();
  };

  const add = useMutation({
    mutationFn: (name: string) =>
      api.post(
        apiPath("/api/communities/{community_id}/members", {
          community_id: communityId,
        }),
        { body: JSON.stringify({ username: name }) },
      ),
    onSuccess: () => {
      showToast(t("communitySettings.members.added"), "success");
      queryClient.invalidateQueries({
        queryKey: membersKeys.list(communityId),
      });
      close();
    },
    onError: (err) => {
      if (onForbidden(err)) {
        close();
        return;
      }
      setError(addErrorMessage(err));
    },
  });

  const submit = () => {
    const name = username.trim();
    if (name.length === 0) {
      setError(t("communitySettings.members.addEmpty"));
      return;
    }
    setError(null);
    add.mutate(name);
  };

  return (
    <Modal
      open={open}
      title={t("communitySettings.members.addDialogTitle")}
      onClose={close}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={close}>
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={add.isPending}
            onClick={submit}
          >
            {t("communitySettings.members.addSubmit")}
          </button>
        </>
      }
    >
      <p>{t("communitySettings.members.addDialogBody")}</p>
      <label className="field">
        {t("communitySettings.members.usernameLabel")}
        <input
          type="text"
          value={username}
          placeholder={t("communitySettings.members.usernamePlaceholder")}
          onChange={(e) => setUsername(e.target.value)}
        />
      </label>
      {error !== null && <span className="field-error">{error}</span>}
    </Modal>
  );
}
