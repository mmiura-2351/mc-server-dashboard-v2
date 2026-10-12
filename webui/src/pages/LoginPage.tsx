import { type FormEvent, useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router";
import { ApiError, api } from "../api/client.ts";
import { useSession } from "../auth/SessionProvider.tsx";
import { PasswordInput } from "../components/PasswordInput.tsx";
import { t } from "../i18n/index.ts";
import { postLoginPath, safeNextPath } from "../routes.ts";

// Use one generic 401 message for unknown users, bad passwords, and lockouts to avoid disclosing account state.
export function LoginPage() {
  const { signIn } = useSession();
  const navigate = useNavigate();
  const from = (useLocation().state as { from?: unknown } | null)?.from;
  const [searchParams] = useSearchParams();
  // Validate the URL return target against open redirects; preserve the expiry notice across reloads.
  const nextPath = safeNextPath(searchParams.get("next"));
  const expired = searchParams.get("reason") === "expired";
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const tokens = await api.post("/api/auth/login", {
        body: JSON.stringify({ username, password }),
      });
      signIn(tokens.access_token);
      navigate(nextPath ?? postLoginPath(from), { replace: true });
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? t("login.invalidCredentials")
          : t("login.genericError"),
      );
      setSubmitting(false);
    }
  }

  return (
    <div className="auth-wrap">
      <form className="card auth-card" onSubmit={onSubmit}>
        <h1 className="visually-hidden">{t("app.title")}</h1>
        <div className="brand">
          <span className="cube" aria-hidden="true" />
          {t("shell.brand")}
        </div>
        {expired ? (
          <div className="notice" role="status">
            {t("login.sessionExpired")}
          </div>
        ) : null}
        <div className="field">
          <label htmlFor="login-username">{t("auth.fieldUsername")}</label>
          <input
            id="login-username"
            type="text"
            autoComplete="username"
            placeholder={t("login.usernamePlaceholder")}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
          />
        </div>
        <div className="field">
          <label htmlFor="login-password">{t("auth.fieldPassword")}</label>
          <PasswordInput
            id="login-password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          {error !== null ? (
            <div className="error" role="alert">
              {error}
            </div>
          ) : null}
        </div>
        <button
          type="submit"
          className="btn primary auth-submit"
          disabled={submitting}
        >
          {submitting ? t("login.submitting") : t("login.submit")}
        </button>
        <div className="alt">
          <Link to="/register">{t("auth.toRegister")}</Link>
        </div>
      </form>
    </div>
  );
}
