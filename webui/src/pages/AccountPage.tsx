import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { ApiError, api } from "../api/client.ts";
import type { components } from "../api/schema";
import { useSession } from "../auth/SessionProvider.tsx";
import { useCurrentUser } from "../auth/useCurrentUser.ts";
import { ConfirmDialog } from "../components/ConfirmDialog.tsx";
import { useToast } from "../components/Toast.tsx";
import { type TranslationKey, t } from "../i18n/index.ts";

type UserResponse = components["schemas"]["UserResponse"];
type CommunityResponse = components["schemas"]["CommunityResponse"];

// Map an API error to a localised message. Every domain error here is an RFC
// 9457 problem+json carrying a machine `reason` (WEBUI_SPEC.md 7.4); the
// account endpoints' reasons are enumerated under the `account.error.` prefix.
// Anything unrecognised falls back to a generic message.
function errorMessage(error: unknown): string {
  if (error instanceof ApiError && error.reason !== undefined) {
    const key = `account.error.${error.reason}` as TranslationKey;
    if (knownErrorKey(key)) {
      return t(key);
    }
  }
  return t("account.error.generic");
}

const KNOWN_ERROR_REASONS = new Set<string>([
  "username_taken",
  "email_taken",
  "invalid_username",
  "invalid_email",
  "invalid_credentials",
  "too_short",
  "too_long",
  "too_long_for_bcrypt",
  "insufficient_complexity",
  "common_password",
  "contains_user_info",
  "simple_pattern",
  "owns_community",
  "last_platform_admin",
]);

function knownErrorKey(key: string): key is TranslationKey {
  return KNOWN_ERROR_REASONS.has(key.replace("account.error.", ""));
}

export function AccountPage() {
  const { showToast } = useToast();

  const userQuery = useCurrentUser();
  const communitiesQuery = useQuery({
    queryKey: ["communities"],
    queryFn: () => api.get("/communities"),
  });

  if (userQuery.isPending) {
    return (
      <div className="account">
        <p className="sub">{t("account.loading")}</p>
      </div>
    );
  }
  if (userQuery.isError || userQuery.data === undefined) {
    return (
      <div className="account">
        <p className="sub">{t("account.loadError")}</p>
      </div>
    );
  }

  return (
    <Loaded
      user={userQuery.data}
      communities={communitiesQuery.data ?? []}
      communitiesError={communitiesQuery.isError}
      showToast={showToast}
    />
  );
}

interface LoadedProps {
  user: UserResponse;
  communities: CommunityResponse[];
  communitiesError: boolean;
  showToast: (message: string, variant: "success" | "error") => void;
}

function Loaded({
  user,
  communities,
  communitiesError,
  showToast,
}: LoadedProps) {
  const { logout } = useSession();
  const queryClient = useQueryClient();

  return (
    <div className="account">
      <div className="page-head">
        <div>
          <h1>{t("page.account")}</h1>
          <div className="sub">{t("account.subtitle")}</div>
        </div>
        <div className="actions">
          <button type="button" className="btn" onClick={() => logout()}>
            {t("account.signOut")}
          </button>
        </div>
      </div>

      <ProfileSection
        user={user}
        showToast={showToast}
        onSaved={() => {
          queryClient.invalidateQueries({ queryKey: ["users", "me"] });
        }}
      />
      <PasswordSection showToast={showToast} />
      <MembershipsSection
        communities={communities}
        isError={communitiesError}
      />
      <DangerZone user={user} showToast={showToast} onDeleted={logout} />
    </div>
  );
}

interface ProfileSectionProps {
  user: UserResponse;
  showToast: LoadedProps["showToast"];
  onSaved: () => void;
}

function ProfileSection({ user, showToast, onSaved }: ProfileSectionProps) {
  const [username, setUsername] = useState(user.username);
  const [email, setEmail] = useState(user.email);
  const [fieldError, setFieldError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () =>
      api.patch("/users/me", { body: JSON.stringify({ username, email }) }),
    onSuccess: () => {
      setFieldError(null);
      showToast(t("account.profile.saved"), "success");
      onSaved();
    },
    onError: (error) => {
      setFieldError(errorMessage(error));
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    mutation.mutate();
  };

  return (
    <form className="card" onSubmit={onSubmit}>
      <h2>{t("account.profile.heading")}</h2>
      <div className="form-row">
        <label className="field">
          {t("account.profile.username")}
          <input
            type="text"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>
        <label className="field">
          {t("account.profile.email")}
          <input
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </label>
      </div>
      {fieldError !== null && <p className="field-error">{fieldError}</p>}
      <button
        type="submit"
        className="btn primary"
        disabled={mutation.isPending}
      >
        {t("account.profile.save")}
      </button>
    </form>
  );
}

function PasswordSection({
  showToast,
}: {
  showToast: LoadedProps["showToast"];
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () =>
      api.put("/users/me/password", {
        body: JSON.stringify({
          current_password: current,
          new_password: next,
        }),
      }),
    onSuccess: () => {
      setError(null);
      setCurrent("");
      setNext("");
      setConfirm("");
      showToast(t("account.password.changed"), "success");
    },
    onError: (err) => {
      setError(errorMessage(err));
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    // Client-side match check before spending a request (mirrors the register
    // form, FR-AUTH-4). The API enforces the real strength policy server-side.
    if (next !== confirm) {
      setError(t("account.password.mismatch"));
      return;
    }
    mutation.mutate();
  };

  return (
    <form className="card" onSubmit={onSubmit}>
      <h2>{t("account.password.heading")}</h2>
      <label className="field">
        {t("account.password.current")}
        <input
          type="password"
          value={current}
          onChange={(event) => setCurrent(event.target.value)}
        />
      </label>
      <div className="form-row">
        <label className="field">
          {t("account.password.new")}
          <input
            type="password"
            value={next}
            onChange={(event) => setNext(event.target.value)}
          />
        </label>
        <label className="field">
          {t("account.password.confirm")}
          <input
            type="password"
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
          />
        </label>
      </div>
      <p className="field-hint">{t("account.password.hint")}</p>
      {error !== null && <p className="field-error">{error}</p>}
      <button
        type="submit"
        className="btn primary"
        disabled={mutation.isPending}
      >
        {t("account.password.change")}
      </button>
    </form>
  );
}

function MembershipsSection({
  communities,
  isError,
}: {
  communities: CommunityResponse[];
  isError: boolean;
}) {
  return (
    <div className="card">
      <h2>{t("account.memberships.heading")}</h2>
      {isError ? (
        <p className="field-error">{t("account.memberships.loadError")}</p>
      ) : communities.length === 0 ? (
        <p className="sub">{t("account.memberships.none")}</p>
      ) : (
        <table className="data">
          <thead>
            <tr>
              <th>{t("account.memberships.community")}</th>
            </tr>
          </thead>
          <tbody>
            {communities.map((community) => (
              <tr key={community.id}>
                <td>{community.name}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

interface DangerZoneProps {
  user: UserResponse;
  showToast: LoadedProps["showToast"];
  onDeleted: () => void;
}

function DangerZone({ user, showToast, onDeleted }: DangerZoneProps) {
  const [open, setOpen] = useState(false);

  const mutation = useMutation({
    // Re-auth: the API verifies the current password before deleting the account
    // (WEBUI_SPEC.md 6.11). A wrong password is a 401 invalid_credentials.
    mutationFn: (password: string) =>
      api.delete("/users/me", { body: JSON.stringify({ password }) }),
    onSuccess: () => {
      setOpen(false);
      // Hard logout: the account is gone, so reset the local session and route
      // to /login (WEBUI_SPEC.md 7.1).
      onDeleted();
    },
    onError: (error) => {
      setOpen(false);
      showToast(errorMessage(error), "error");
    },
  });

  return (
    <div className="card danger-zone">
      <h2>{t("account.delete.heading")}</h2>
      <div className="row">
        <div>
          <strong>{t("account.delete.label")}</strong>
          <div className="desc">{t("account.delete.desc")}</div>
        </div>
        <button
          type="button"
          className="btn danger"
          onClick={() => setOpen(true)}
        >
          {t("account.delete.open")}
        </button>
      </div>

      <ConfirmDialog
        open={open}
        title={t("account.delete.dialogTitle")}
        body={t("account.delete.dialogBody")}
        confirmPhrase={user.username}
        confirmLabel={t("account.delete.confirm")}
        promptLabel={t("account.delete.prompt")}
        passwordLabel={t("account.delete.password")}
        onConfirm={(password) => mutation.mutate(password)}
        onClose={() => setOpen(false)}
      />
    </div>
  );
}
