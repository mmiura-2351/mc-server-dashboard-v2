import { type FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router";
import { ApiError, api } from "../api/client.ts";
import { fieldErrorsFromValidation } from "../api/validationErrors.ts";
import { useSession } from "../auth/SessionProvider.tsx";
import { PasswordInput } from "../components/PasswordInput.tsx";
import { useToast } from "../components/Toast.tsx";
import { type TranslationKey, t } from "../i18n/index.ts";
import { LANDING_PATH } from "../routes.ts";

// Try auto-login after registration; if it fails, send the user to login with a registration-success notice.

const MIN_PASSWORD_LENGTH = 12;

interface FieldErrors {
  username?: string;
  email?: string;
  password?: string;
}

const REASON_KEY: Record<string, TranslationKey> = {
  too_short: "register.reason.too_short",
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

function fieldForReason(reason: string): keyof FieldErrors {
  if (reason === "username_taken" || reason === "invalid_username") {
    return "username";
  }
  if (reason === "email_taken" || reason === "invalid_email") {
    return "email";
  }
  return "password";
}

// Empty username/email can pass local validation; map the API's structural 422 entries to those fields.
const VALIDATION_FIELDS = ["username", "email", "password"] as const;

function localValidate(
  username: string,
  email: string,
  password: string,
  confirm: string,
): FieldErrors {
  const errors: FieldErrors = {};
  if (password.length < MIN_PASSWORD_LENGTH) {
    errors.password = t("register.reason.too_short");
  } else if (
    (username !== "" &&
      password.toLowerCase().includes(username.toLowerCase())) ||
    (email !== "" && password.toLowerCase().includes(email.toLowerCase()))
  ) {
    errors.password = t("register.reason.contains_user_info");
  } else if (confirm !== password) {
    errors.password = t("register.errPasswordMismatch");
  }
  return errors;
}

export function RegisterPage() {
  const navigate = useNavigate();
  const { signIn } = useSession();
  const { showToast } = useToast();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<FieldErrors>({});
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const local = localValidate(username, email, password, confirm);
    if (local.password !== undefined) {
      setErrors(local);
      return;
    }
    setErrors({});
    setSubmitting(true);
    try {
      await api.post("/api/users", {
        body: JSON.stringify({ username, email, password }),
      });
      // Keep registration successful even if auto-login fails; manual login remains available.
      try {
        const tokens = await api.post("/api/auth/login", {
          body: JSON.stringify({ username, password }),
        });
        signIn(tokens.access_token);
        navigate(LANDING_PATH, { replace: true });
      } catch {
        showToast(t("register.success"), "success");
        navigate("/login", { replace: true });
      }
    } catch (err) {
      if (err instanceof ApiError && err.reason !== undefined) {
        const messageKey = REASON_KEY[err.reason];
        if (messageKey !== undefined) {
          setErrors({ [fieldForReason(err.reason)]: t(messageKey) });
          setSubmitting(false);
          return;
        }
        if (err.reason === "validation_error") {
          const fieldErrors = fieldErrorsFromValidation(
            err.body,
            VALIDATION_FIELDS,
          );
          if (fieldErrors !== null) {
            setErrors(fieldErrors);
            setSubmitting(false);
            return;
          }
        }
      }
      showToast(t("register.genericError"), "error");
      setSubmitting(false);
    }
  }

  return (
    <div className="auth-wrap">
      <form className="card auth-card" onSubmit={onSubmit} noValidate>
        <h1 className="visually-hidden">{t("app.title")}</h1>
        <div className="brand">
          <span className="cube" aria-hidden="true" />
          {t("shell.brand")}
        </div>
        <div className="field">
          <label htmlFor="register-username">{t("auth.fieldUsername")}</label>
          <input
            id="register-username"
            type="text"
            autoComplete="username"
            placeholder={t("register.usernamePlaceholder")}
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
          <label htmlFor="register-email">{t("auth.fieldEmail")}</label>
          <input
            id="register-email"
            type="email"
            autoComplete="email"
            placeholder={t("register.emailPlaceholder")}
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
          <label htmlFor="register-password">{t("auth.fieldPassword")}</label>
          <PasswordInput
            id="register-password"
            autoComplete="new-password"
            placeholder={t("register.passwordPlaceholder")}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          <div className="hint">{t("register.passwordHint")}</div>
          {errors.password !== undefined ? (
            <div className="error" role="alert">
              {errors.password}
            </div>
          ) : null}
        </div>
        <div className="field">
          <label htmlFor="register-confirm">
            {t("register.confirmPassword")}
          </label>
          <PasswordInput
            id="register-confirm"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            required
          />
        </div>
        <button
          type="submit"
          className="btn primary auth-submit"
          disabled={submitting}
        >
          {submitting ? t("register.submitting") : t("register.submit")}
        </button>
        <div className="alt">
          <Link to="/login">{t("auth.toLogin")}</Link>
        </div>
      </form>
    </div>
  );
}
