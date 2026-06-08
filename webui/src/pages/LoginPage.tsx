import { type FormEvent, useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router";
import { ApiError, api } from "../api/client.ts";
import { useSession } from "../auth/SessionProvider.tsx";
import { PasswordInput } from "../components/PasswordInput.tsx";
import { t } from "../i18n/index.ts";
import { postLoginPath, safeNextPath } from "../routes.ts";

// Login page (WEBUI_SPEC.md 6.1). Posts credentials to /auth/login; the API
// returns the token pair and sets the refresh cookie. We adopt the access token
// through the session layer and land on the dashboard. A 401 collapses every
// failure mode (unknown user / wrong password / lockout) to one generic inline
// message — the API leaks no distinguishing detail (AUTH_API.md 1).
export function LoginPage() {
  const { signIn } = useSession();
  const navigate = useNavigate();
  const from = (useLocation().state as { from?: unknown } | null)?.from;
  const [searchParams] = useSearchParams();
  // The session-expiry flow (#565) carries the return-to target and the reason
  // on the URL so a refresh/bookmark of /login stays consistent. `next` is
  // validated against open redirects; `expired` drives the explanatory notice.
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
      // A validated `next` (session-expiry return-to) wins over the #424 guard
      // stash; both fall back to the post-login landing.
      navigate(nextPath ?? postLoginPath(from), { replace: true });
    } catch (err) {
      // 401 is the only credential outcome; everything else is a generic fault.
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
