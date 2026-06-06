import { type FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router";
import { ApiError, api } from "../api/client.ts";
import { useToast } from "../components/Toast.tsx";
import { type TranslationKey, t } from "../i18n/index.ts";

// Register page (WEBUI_SPEC.md 6.1). Posts to /users; on success routes to
// /login with a success toast (no auto-login — keeping the honest "register
// then sign in" flow). The server is authoritative on password strength; the
// client only mirrors the FR-AUTH-4 hints to catch the obvious cases before a
// round-trip. 422 reason codes surface inline against the relevant field; other
// failures (registration closed / throttled / unexpected) go to a toast.

const MIN_PASSWORD_LENGTH = 12;

interface FieldErrors {
  username?: string;
  email?: string;
  password?: string;
}

// Map a server problem+json reason to its inline message and field. Reasons the
// register endpoint emits are enumerated in users.py (register_user).
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

// A structural FastAPI/Pydantic 422 (reason "validation_error") carries a
// per-field `errors` list (AUTH_API.md 2; entry shape `loc`/`msg`/`type` after
// the #393/#395 scrub of `input`/`ctx`). The reachable path: an empty
// username/email with a long-enough password clears localValidate, so Pydantic
// `min_length=1` rejects it server-side (#410). Map each entry to its form
// field by the `loc` tail (`["body", "<field>"]`) and use the validator `msg`
// verbatim. Returns the inline errors, or null when no entry maps to a known
// field — the caller then falls back to the generic toast.
interface ValidationEntry {
  loc: unknown[];
  msg: string;
}

function fieldErrorsFromValidation(body: unknown): FieldErrors | null {
  if (typeof body !== "object" || body === null || !("errors" in body)) {
    return null;
  }
  const { errors } = body as { errors: unknown };
  if (!Array.isArray(errors)) {
    return null;
  }
  const known: Record<string, keyof FieldErrors> = {
    username: "username",
    email: "email",
    password: "password",
  };
  const mapped: FieldErrors = {};
  for (const entry of errors as ValidationEntry[]) {
    const field = entry.loc?.[entry.loc.length - 1];
    if (
      typeof field === "string" &&
      field in known &&
      typeof entry.msg === "string"
    ) {
      const key = known[field];
      if (mapped[key] === undefined) {
        mapped[key] = entry.msg;
      }
    }
  }
  return Object.keys(mapped).length > 0 ? mapped : null;
}

// Client-side mirror of the FR-AUTH-4 hints; the server remains authoritative.
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
      await api.post("/users", {
        body: JSON.stringify({ username, email, password }),
      });
      showToast(t("register.success"), "success");
      navigate("/login", { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.reason !== undefined) {
        const messageKey = REASON_KEY[err.reason];
        if (messageKey !== undefined) {
          setErrors({ [fieldForReason(err.reason)]: t(messageKey) });
          setSubmitting(false);
          return;
        }
        if (err.reason === "validation_error") {
          const fieldErrors = fieldErrorsFromValidation(err.body);
          if (fieldErrors !== null) {
            setErrors(fieldErrors);
            setSubmitting(false);
            return;
          }
        }
      }
      // Registration closed / throttled / anything unmapped: generic toast.
      showToast(t("register.genericError"), "error");
      setSubmitting(false);
    }
  }

  return (
    <div className="auth-wrap">
      <form className="card auth-card" onSubmit={onSubmit} noValidate>
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
          <input
            id="register-password"
            type="password"
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
          <input
            id="register-confirm"
            type="password"
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
