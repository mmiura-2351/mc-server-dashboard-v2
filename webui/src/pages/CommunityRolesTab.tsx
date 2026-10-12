import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client.ts";
import { membersKeys, rolesKeys } from "../api/communityQueryKeys.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { Modal } from "../components/Modal.tsx";
import { SimpleConfirmDialog } from "../components/SimpleConfirmDialog.tsx";
import { useToast } from "../components/Toast.tsx";
import { type TranslationKey, t } from "../i18n/index.ts";
import {
  COMMUNITY_PERMISSION_FAMILIES,
  type CommunityPermissionCode,
} from "../permissions/catalog.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { classifyQueryResult } from "../queryState.ts";

type RoleResponse = components["schemas"]["RoleResponse"];

export function CommunityRolesTab({
  communityId,
  can,
}: {
  communityId: string;
  can: Can;
}) {
  const canManage = can("role:manage");
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<RoleResponse | "new" | null>(null);
  const [deleting, setDeleting] = useState<RoleResponse | null>(null);

  // Role edits also change member role chips, so invalidate both list projections.
  const invalidateRoles = () => {
    queryClient.invalidateQueries({ queryKey: rolesKeys.list(communityId) });
    queryClient.invalidateQueries({ queryKey: membersKeys.list(communityId) });
  };

  const roles = useQuery({
    queryKey: rolesKeys.list(communityId),
    queryFn: ({ signal }) =>
      api.get(
        apiPath("/api/communities/{community_id}/roles", {
          community_id: communityId,
        }),
        { signal },
      ),
  });

  const remove = useMutation({
    mutationFn: (role: RoleResponse) =>
      api.delete(
        apiPath("/api/communities/{community_id}/roles/{role_id}", {
          community_id: communityId,
          role_id: role.id,
        }),
      ),
    onSuccess: () => {
      showToast(t("communitySettings.roles.deleted"), "success");
      invalidateRoles();
      setDeleting(null);
    },
    onError: (error) => {
      setDeleting(null);
      if (onForbidden(error)) {
        return;
      }
      showToast(t("communitySettings.roles.deleteError"), "error");
    },
  });

  const rolesState = classifyQueryResult(roles);

  if (rolesState.kind === "pending") {
    return <p className="sub">{t("communitySettings.roles.loading")}</p>;
  }
  // Retain cached content when only a background refetch fails.
  if (rolesState.kind === "error") {
    return (
      <p className="field-error">{t("communitySettings.roles.loadError")}</p>
    );
  }

  return (
    <section>
      <div className="page-head">
        <h2>{t("communitySettings.roles.heading")}</h2>
        {canManage && (
          <button
            type="button"
            className="btn primary"
            onClick={() => setEditing("new")}
          >
            {t("communitySettings.roles.create")}
          </button>
        )}
      </div>

      {rolesState.data.length === 0 ? (
        <p className="sub">{t("communitySettings.roles.empty")}</p>
      ) : (
        <table className="data">
          <tbody>
            {rolesState.data.map((role) => (
              <tr key={role.id}>
                <td>
                  {role.name}
                  {role.is_preset && (
                    <span className="badge">
                      {t("communitySettings.roles.preset")}
                    </span>
                  )}
                </td>
                <td className="roles-actions">
                  {canManage && !role.is_preset && (
                    <>
                      <button
                        type="button"
                        className="btn sm"
                        onClick={() => setEditing(role)}
                      >
                        {t("communitySettings.roles.edit")}
                      </button>
                      <button
                        type="button"
                        className="btn sm danger"
                        onClick={() => setDeleting(role)}
                      >
                        {t("communitySettings.roles.delete")}
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {canManage && editing !== null && (
        <RoleEditor
          communityId={communityId}
          role={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
        />
      )}

      <SimpleConfirmDialog
        open={deleting !== null}
        title={t("communitySettings.roles.deleteDialogTitle")}
        body={t("communitySettings.roles.deleteDialogBody")}
        confirmLabel={t("communitySettings.roles.deleteConfirm")}
        busy={remove.isPending}
        onConfirm={() => {
          if (deleting !== null) {
            remove.mutate(deleting);
          }
        }}
        onClose={() => setDeleting(null)}
      />
    </section>
  );
}

function saveErrorMessage(error: unknown): TranslationKey {
  if (error instanceof ApiError) {
    if (error.reason === "name_taken") {
      return "communitySettings.roles.errNameTaken";
    }
    if (error.reason === "invalid_name") {
      return "communitySettings.roles.errInvalidName";
    }
    if (error.reason === "preset_role") {
      return "communitySettings.roles.errPreset";
    }
  }
  return "communitySettings.roles.errGeneric";
}

function RoleEditor({
  communityId,
  role,
  onClose,
}: {
  communityId: string;
  role: RoleResponse | null;
  onClose: () => void;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const [name, setName] = useState(role?.name ?? "");
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(role?.permissions ?? []),
  );
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (body: { name: string; permissions: string[] }) =>
      role === null
        ? api.post(
            apiPath("/api/communities/{community_id}/roles", {
              community_id: communityId,
            }),
            { body: JSON.stringify(body) },
          )
        : api.patch(
            apiPath("/api/communities/{community_id}/roles/{role_id}", {
              community_id: communityId,
              role_id: role.id,
            }),
            { body: JSON.stringify(body) },
          ),
    onSuccess: () => {
      showToast(
        role === null
          ? t("communitySettings.roles.created")
          : t("communitySettings.roles.updated"),
        "success",
      );
      // Renaming changes member role chips too; refresh both list projections.
      queryClient.invalidateQueries({ queryKey: rolesKeys.list(communityId) });
      queryClient.invalidateQueries({
        queryKey: membersKeys.list(communityId),
      });
      onClose();
    },
    onError: (err) => {
      if (onForbidden(err)) {
        onClose();
        return;
      }
      setError(t(saveErrorMessage(err)));
    },
  });

  const toggle = (code: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(code)) {
        next.delete(code);
      } else {
        next.add(code);
      }
      return next;
    });
  };

  const toggleFamily = (codes: readonly string[], allOn: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const code of codes) {
        if (allOn) {
          next.delete(code);
        } else {
          next.add(code);
        }
      }
      return next;
    });
  };

  const submit = () => {
    const trimmed = name.trim();
    if (trimmed.length === 0) {
      setError(t("communitySettings.roles.nameEmpty"));
      return;
    }
    setError(null);
    save.mutate({ name: trimmed, permissions: [...selected] });
  };

  return (
    <Modal
      open
      title={t(
        role === null
          ? "communitySettings.roles.createDialogTitle"
          : "communitySettings.roles.editDialogTitle",
      )}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={save.isPending}
            onClick={submit}
          >
            {t("communitySettings.roles.save")}
          </button>
        </>
      }
    >
      <label className="field">
        {t("communitySettings.roles.nameLabel")}
        <input
          type="text"
          value={name}
          placeholder={t("communitySettings.roles.namePlaceholder")}
          onChange={(e) => setName(e.target.value)}
        />
      </label>

      <div className="roles-matrix">
        <span className="field-label">
          {t("communitySettings.roles.permissionsLabel")}
        </span>
        {COMMUNITY_PERMISSION_FAMILIES.map(({ family, codes }) => {
          const allOn = codes.every((code) => selected.has(code));
          return (
            <fieldset key={family} className="roles-family">
              <legend>
                <label>
                  <input
                    type="checkbox"
                    aria-label={`${t("communitySettings.roles.selectAll")}: ${t(`communitySettings.roles.family.${family}`)}`}
                    checked={allOn}
                    onChange={() => toggleFamily(codes, allOn)}
                  />
                  {t(`communitySettings.roles.family.${family}`)}
                </label>
              </legend>
              {codes.map((code) => (
                <label key={code} className="roles-perm">
                  <input
                    type="checkbox"
                    // Show the full code because several permission families use the same action label.
                    aria-label={code}
                    checked={selected.has(code)}
                    onChange={() => toggle(code)}
                  />
                  {t(permissionLabelKey(code))}
                </label>
              ))}
            </fieldset>
          );
        })}
      </div>

      {error !== null && <span className="field-error">{error}</span>}
    </Modal>
  );
}

function permissionLabelKey(code: CommunityPermissionCode): TranslationKey {
  return `communitySettings.roles.code.${code}`;
}
