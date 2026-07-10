import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { ApiError, api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { useCurrentUser } from "../auth/useCurrentUser.ts";
import { ConfirmDialog } from "../components/ConfirmDialog.tsx";
import { Modal } from "../components/Modal.tsx";
import { PasswordInput } from "../components/PasswordInput.tsx";
import { ResizableTable } from "../components/ResizableColumns.tsx";
import { useToast } from "../components/Toast.tsx";
import { type TranslationKey, t } from "../i18n/index.ts";
import { useOffsetParam } from "./urlState.ts";

// Platform admin Users page (WEBUI_SPEC.md 6.12 / 2.1): a paginated user table
// with the full lifecycle an admin owns — deactivate/reactivate, grant/revoke
// the platform-admin flag, delete (typed-confirm), and create. All routes are
// the platform-admin `[A]` surface (admin_users.py).
//
// Self-action behaviour, surfaced honestly per the API guards (admin_users.py):
// - deactivate / delete yourself → 409 `self_target` (refused; use the account
//   page). The row hides those actions for your own account and a stray 409
//   still surfaces as the mapped message.
// - revoke your own admin flag → ALLOWED by the API (only the last active admin
//   is protected). We confirm first, since it locks the operator out of /admin.

type AdminUserResponse = components["schemas"]["AdminUserResponse"];

// The API caps a single page at 100 and defaults to 50 (admin_users.py).
const PAGE_SIZE = 50;

function usersUrl(offset: number): "/api/admin/users" {
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(offset),
  });
  return `/api/admin/users?${params.toString()}` as "/api/admin/users";
}

// Map an admin-create problem `reason` to the inline message + field. Password
// policy reasons reuse the register copy (SECURITY.md Section 1 enumerates them,
// shared with `POST /users`); duplicates/invalid map to their field.
const CREATE_REASON_KEY: Record<string, TranslationKey> = {
  too_short: "register.reason.too_short",
  too_long: "register.reason.too_long_for_bcrypt",
  too_long_for_bcrypt: "register.reason.too_long_for_bcrypt",
  insufficient_complexity: "register.reason.insufficient_complexity",
  common_password: "register.reason.common_password",
  contains_user_info: "register.reason.contains_user_info",
  simple_pattern: "register.reason.simple_pattern",
  username_taken: "register.reason.username_taken",
  email_taken: "register.reason.email_taken",
  invalid_username: "register.reason.invalid_username",
  invalid_email: "register.reason.invalid_email",
};

interface CreateFieldErrors {
  username?: string;
  email?: string;
  password?: string;
  // Fallback for an unmapped reason or a non-problem failure (500/network) so
  // the dialog never sits silent (#475).
  general?: string;
}

function createFieldForReason(reason: string): keyof CreateFieldErrors {
  if (reason === "username_taken" || reason === "invalid_username") {
    return "username";
  }
  if (reason === "email_taken" || reason === "invalid_email") {
    return "email";
  }
  return "password";
}

// Conflict / not-found reasons the lifecycle routes return (admin_users.py);
// anything unmapped falls back to the generic message.
const LIFECYCLE_REASON_KEY: Record<string, TranslationKey> = {
  self_target: "admin.users.error.self_target",
  last_platform_admin: "admin.users.error.last_platform_admin",
  owns_community: "admin.users.error.owns_community",
  not_found: "admin.users.error.not_found",
};

function lifecycleErrorMessage(err: unknown): string {
  if (err instanceof ApiError && err.reason !== undefined) {
    const key = LIFECYCLE_REASON_KEY[err.reason];
    if (key !== undefined) {
      return t(key);
    }
  }
  return t("admin.users.error.generic");
}

export function AdminUsersPage() {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { data: me } = useCurrentUser();
  // Page offset lives in `?offset=N` (#514) so Back restores the prior page.
  const [offset, setOffset] = useOffsetParam();
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<AdminUserResponse | null>(
    null,
  );
  const [selfRevoke, setSelfRevoke] = useState(false);
  // The user whose lifecycle mutation is in flight; its row's actions are
  // disabled until it settles so a second click can't double-fire (#475).
  const [pendingUserId, setPendingUserId] = useState<string | null>(null);

  const usersQuery = useQuery({
    queryKey: ["users", "list", offset],
    queryFn: ({ signal }) => api.get(usersUrl(offset), { signal }),
    placeholderData: keepPreviousData,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["users", "list"] });
    // The current-user cache changes when an admin revokes their own flag.
    queryClient.invalidateQueries({ queryKey: ["users", "me"] });
  };

  const runLifecycle = async (
    userId: string,
    action: () => Promise<unknown>,
    successKey: TranslationKey,
  ) => {
    if (pendingUserId !== null) {
      return;
    }
    setPendingUserId(userId);
    try {
      await action();
      showToast(t(successKey), "success");
      invalidate();
    } catch (err) {
      showToast(lifecycleErrorMessage(err), "error");
    } finally {
      setPendingUserId(null);
    }
  };

  const setActive = (user: AdminUserResponse, active: boolean) => {
    const path = apiPath(
      active
        ? "/api/admin/users/{user_id}/reactivate"
        : "/api/admin/users/{user_id}/deactivate",
      { user_id: user.id },
    );
    runLifecycle(
      user.id,
      () => api.post(path),
      active ? "admin.users.reactivated" : "admin.users.deactivated",
    );
  };

  const setAdmin = (user: AdminUserResponse, grant: boolean) => {
    runLifecycle(
      user.id,
      () =>
        api.put(
          apiPath("/api/admin/users/{user_id}/platform-admin", {
            user_id: user.id,
          }),
          { body: JSON.stringify({ grant }) },
        ),
      grant ? "admin.users.adminGranted" : "admin.users.adminRevoked",
    );
  };

  const onToggleAdmin = (user: AdminUserResponse) => {
    // Revoking your own flag is permitted by the API but locks you out of the
    // admin area, so gate it behind a confirm.
    if (user.is_platform_admin && user.id === me?.id) {
      setSelfRevoke(true);
      return;
    }
    setAdmin(user, !user.is_platform_admin);
  };

  const onDelete = () => {
    if (deleteTarget === null) {
      return;
    }
    const target = deleteTarget;
    setDeleteTarget(null);
    runLifecycle(
      target.id,
      () =>
        api.delete(
          apiPath("/api/admin/users/{user_id}", { user_id: target.id }),
        ),
      "admin.users.deleted",
    );
  };

  const total = usersQuery.data?.total ?? 0;
  const users = usersQuery.data?.users ?? [];
  const hasNext = offset + PAGE_SIZE < total;
  const rangeFrom = total === 0 ? 0 : offset + 1;
  const rangeTo = offset + users.length;

  return (
    <div className="admin-users">
      <div className="page-head">
        <div>
          <h1>{t("page.adminUsers")}</h1>
          <div className="sub">
            {total} {t("admin.users.count")}
          </div>
        </div>
        <button
          type="button"
          className="btn primary"
          onClick={() => setCreateOpen(true)}
        >
          {t("admin.users.create")}
        </button>
      </div>

      {usersQuery.isPending ? (
        <p className="sub" role="status">
          {t("admin.users.loading")}
        </p>
      ) : usersQuery.isError ? (
        <p className="field-error" role="alert">
          {t("admin.users.loadError")}
        </p>
      ) : users.length === 0 ? (
        <p className="sub">{t("admin.users.empty")}</p>
      ) : (
        <div className="card" style={{ padding: 0 }}>
          <ResizableTable storageKey="mcsd.colw.admin-users" className="data">
            <thead>
              <tr>
                <th>{t("admin.users.colUsername")}</th>
                <th>{t("admin.users.colEmail")}</th>
                <th>{t("admin.users.colStatus")}</th>
                <th>{t("admin.users.colAdmin")}</th>
                <th>{t("admin.users.colCreated")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <UserRow
                  key={user.id}
                  user={user}
                  isSelf={user.id === me?.id}
                  pending={pendingUserId === user.id}
                  onToggleActive={() => setActive(user, !user.active)}
                  onToggleAdmin={() => onToggleAdmin(user)}
                  onDelete={() => setDeleteTarget(user)}
                />
              ))}
            </tbody>
          </ResizableTable>
        </div>
      )}

      <div className="users-paging">
        <button
          type="button"
          className="btn sm ghost"
          disabled={offset === 0 || usersQuery.isFetching}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        >
          {t("admin.users.prev")}
        </button>
        <span className="sub">
          {t("admin.users.range", {
            from: rangeFrom,
            to: rangeTo,
            total,
          })}
        </span>
        <button
          type="button"
          className="btn sm ghost"
          disabled={!hasNext || usersQuery.isFetching}
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          {t("admin.users.next")}
        </button>
      </div>

      <CreateUserDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={() => {
          setCreateOpen(false);
          showToast(t("admin.users.created"), "success");
          invalidate();
        }}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        title={t("admin.users.deleteTitle")}
        body={t("admin.users.deleteBody")}
        confirmPhrase={deleteTarget?.username ?? ""}
        confirmLabel={t("admin.users.deleteConfirm")}
        promptLabel={t("admin.users.deletePrompt")}
        onConfirm={onDelete}
        onClose={() => setDeleteTarget(null)}
      />

      <Modal
        open={selfRevoke}
        title={t("admin.users.selfRevokeTitle")}
        onClose={() => setSelfRevoke(false)}
        footer={
          <>
            <button
              type="button"
              className="btn ghost"
              onClick={() => setSelfRevoke(false)}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn danger"
              onClick={() => {
                setSelfRevoke(false);
                if (me !== undefined) {
                  setAdmin(me as AdminUserResponse, false);
                }
              }}
            >
              {t("admin.users.selfRevokeConfirm")}
            </button>
          </>
        }
      >
        <p>{t("admin.users.selfRevokeBody")}</p>
      </Modal>
    </div>
  );
}

function UserRow({
  user,
  isSelf,
  pending,
  onToggleActive,
  onToggleAdmin,
  onDelete,
}: {
  user: AdminUserResponse;
  isSelf: boolean;
  pending: boolean;
  onToggleActive: () => void;
  onToggleAdmin: () => void;
  onDelete: () => void;
}) {
  return (
    <tr style={user.active ? undefined : { opacity: 0.55 }}>
      <td>
        <strong>{user.username}</strong>
        {isSelf ? (
          <span className="badge admin"> {t("admin.users.you")}</span>
        ) : null}
      </td>
      <td className="dim" title={user.email}>
        {user.email}
      </td>
      <td>
        <span className={`pill ${user.active ? "running" : "stopped"}`}>
          {user.active
            ? t("admin.users.statusActive")
            : t("admin.users.statusDeactivated")}
        </span>
      </td>
      <td>
        {user.is_platform_admin ? (
          <span className="badge admin">{t("admin.users.adminYes")}</span>
        ) : (
          t("admin.users.adminNo")
        )}
      </td>
      <td className="num">{new Date(user.created_at).toLocaleDateString()}</td>
      <td className="row-actions">
        <button
          type="button"
          className="btn sm"
          disabled={pending}
          onClick={onToggleAdmin}
        >
          {user.is_platform_admin
            ? t("admin.users.revokeAdmin")
            : t("admin.users.makeAdmin")}
        </button>
        {/* Deactivate/delete of your own account are 409 self_target on the API;
            hide them for your own row (reactivate is unreachable for self). */}
        {isSelf ? null : (
          <>
            <button
              type="button"
              className="btn sm"
              disabled={pending}
              onClick={onToggleActive}
            >
              {user.active
                ? t("admin.users.deactivate")
                : t("admin.users.reactivate")}
            </button>
            <button
              type="button"
              className="btn sm danger"
              disabled={pending}
              onClick={onDelete}
            >
              {t("admin.users.delete")}
            </button>
          </>
        )}
      </td>
    </tr>
  );
}

function CreateUserDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<CreateFieldErrors>({});

  const reset = () => {
    setUsername("");
    setEmail("");
    setPassword("");
    setErrors({});
  };

  const mutation = useMutation({
    mutationFn: () =>
      api.post("/api/admin/users", {
        body: JSON.stringify({ username, email, password }),
      }),
    onSuccess: () => {
      reset();
      onCreated();
    },
    onError: (err) => {
      // Map a problem `reason` to its inline field; structural 422s are not
      // reachable here (all three fields are required and non-empty). An
      // unmapped reason or a non-problem failure (500/network) falls back to a
      // generic inline error so the dialog never sits silent.
      if (err instanceof ApiError && err.reason !== undefined) {
        const key = CREATE_REASON_KEY[err.reason];
        if (key !== undefined) {
          setErrors({ [createFieldForReason(err.reason)]: t(key) });
          return;
        }
      }
      setErrors({ general: t("admin.users.error.generic") });
    },
  });

  const close = () => {
    reset();
    mutation.reset();
    onClose();
  };

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    setErrors({});
    mutation.mutate();
  };

  return (
    <Modal
      open={open}
      title={t("admin.users.createTitle")}
      onClose={close}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={close}>
            {t("common.cancel")}
          </button>
          <button
            type="submit"
            form="admin-create-user"
            className="btn primary"
            disabled={mutation.isPending}
          >
            {mutation.isPending
              ? t("admin.users.createSubmitting")
              : t("admin.users.createSubmit")}
          </button>
        </>
      }
    >
      <form id="admin-create-user" onSubmit={onSubmit} noValidate>
        {errors.general !== undefined ? (
          <div className="error" role="alert">
            {errors.general}
          </div>
        ) : null}
        <div className="field">
          <label htmlFor="admin-create-username">
            {t("admin.users.usernameLabel")}
          </label>
          <input
            id="admin-create-username"
            type="text"
            autoComplete="off"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
          />
          {errors.username !== undefined ? (
            <div className="error" role="alert">
              {errors.username}
            </div>
          ) : null}
        </div>
        <div className="field">
          <label htmlFor="admin-create-email">
            {t("admin.users.emailLabel")}
          </label>
          <input
            id="admin-create-email"
            type="email"
            autoComplete="off"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
          {errors.email !== undefined ? (
            <div className="error" role="alert">
              {errors.email}
            </div>
          ) : null}
        </div>
        <div className="field">
          <label htmlFor="admin-create-password">
            {t("admin.users.passwordLabel")}
          </label>
          <PasswordInput
            id="admin-create-password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          <div className="hint">{t("admin.users.passwordHint")}</div>
          {errors.password !== undefined ? (
            <div className="error" role="alert">
              {errors.password}
            </div>
          ) : null}
        </div>
      </form>
    </Modal>
  );
}
