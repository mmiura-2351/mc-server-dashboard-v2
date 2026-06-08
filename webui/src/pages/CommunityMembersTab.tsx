import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client.ts";
import { membersKeys, rolesKeys } from "../api/communityQueryKeys.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { ConfirmDialog } from "../components/ConfirmDialog.tsx";
import { Modal } from "../components/Modal.tsx";
import { useToast } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";

type MemberResponse = components["schemas"]["MemberResponse"];
type RoleResponse = components["schemas"]["RoleResponse"];

// Members tab (WEBUI_SPEC.md 6.10): table of username + role chips, add by
// exact username, inline role assign/unassign, remove with a revocation
// confirm. Role assign/unassign are gated by `role:manage` (the API gates them
// there — assigning a role changes what a member can do), the choices come from
// `role:read`, and add/remove use `member:add`/`member:remove`.
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
    queryFn: () =>
      api.get(
        apiPath("/api/communities/{community_id}/members", {
          community_id: communityId,
        }),
      ),
  });

  // Role choices for the assign picker; only fetched when the caller can both
  // read roles and manage them (otherwise the picker is hidden anyway).
  const roles = useQuery({
    queryKey: rolesKeys.list(communityId),
    queryFn: () =>
      api.get(
        apiPath("/api/communities/{community_id}/roles", {
          community_id: communityId,
        }),
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

  if (members.isPending) {
    return <p className="sub">{t("communitySettings.members.loading")}</p>;
  }
  if (members.isError || members.data === undefined) {
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

      {members.data.length === 0 ? (
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
            {members.data.map((member) => (
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

      <ConfirmDialog
        open={removing !== null}
        title={t("communitySettings.members.removeDialogTitle")}
        body={t("communitySettings.members.removeDialogBody")}
        confirmPhrase={removing?.username ?? ""}
        confirmLabel={t("communitySettings.members.removeConfirm")}
        promptLabel={t("communitySettings.members.removePrompt")}
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

  // Map a held role name back to its id so a chip can unassign it. A name with
  // no matching role in the list (none expected in normal operation) is shown
  // as a non-removable chip.
  const roleByName = new Map(roles.map((r) => [r.name, r]));
  // Roles the member does not yet hold, offered in the assign picker.
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

// Map the add-member rejection reason to a specific inline message: a missing
// user or a non-member username is 422 `user_not_found`; an existing membership
// is 409 `already_member` (members.py; #355 contract).
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
