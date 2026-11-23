import { type FormEvent, useState } from "react";
import { t } from "../i18n/index.ts";
import { Modal } from "./Modal.tsx";
import { PasswordInput } from "./PasswordInput.tsx";

// Typed-confirm dialog for destructive operations (WEBUI_SPEC.md Section 7.4).
// The destructive button stays disabled until the user types confirmPhrase
// exactly (case-sensitive), guarding deletes and other irreversible actions.
//
// When passwordLabel is set, the dialog also re-authenticates: a password field
// is rendered and the button additionally requires a non-empty password, which
// is passed to onConfirm. Account deletion uses this (WEBUI_SPEC.md 6.11); the
// other destructive flows omit it and the field never renders.
//
// The password field then sits in a form, so Enter in it confirms through the
// confirm button, and password managers see a sign-in form: the field is named
// and, when the caller passes the signed-in username, a hidden read-only
// username field names the account (#4121, as on the change-password form).
// The typed phrase stays outside that form.

const PASSWORD_FORM_ID = "confirm-dialog-password-form";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  body: string;
  confirmPhrase: string;
  confirmLabel: string;
  promptLabel: string;
  passwordLabel?: string;
  username?: string;
  onConfirm: (password: string) => void;
  onClose: () => void;
}

export function ConfirmDialog({
  open,
  title,
  body,
  confirmPhrase,
  confirmLabel,
  promptLabel,
  passwordLabel,
  username,
  onConfirm,
  onClose,
}: ConfirmDialogProps) {
  const [typed, setTyped] = useState("");
  const [password, setPassword] = useState("");
  // Both gates must pass: the typed phrase matches and, when re-auth is
  // required, a password has been entered (the API verifies it server-side).
  const matches =
    typed === confirmPhrase && (passwordLabel === undefined || password !== "");

  const close = () => {
    setTyped("");
    setPassword("");
    onClose();
  };

  const confirm = () => {
    setTyped("");
    setPassword("");
    onConfirm(password);
  };

  // A password manager may submit the form with the phrase still untyped, so
  // the gate holds here too; the submit never leaves the page.
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (matches) {
      confirm();
    }
  };

  return (
    <Modal
      open={open}
      title={title}
      onClose={close}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={close}>
            {t("common.cancel")}
          </button>
          {passwordLabel === undefined ? (
            <button
              type="button"
              className="btn danger"
              disabled={!matches}
              onClick={confirm}
            >
              {confirmLabel}
            </button>
          ) : (
            <button
              type="submit"
              form={PASSWORD_FORM_ID}
              className="btn danger"
              disabled={!matches}
            >
              {confirmLabel}
            </button>
          )}
        </>
      }
    >
      <p>{body}</p>
      <label className="field">
        {promptLabel}
        <input
          type="text"
          value={typed}
          placeholder={confirmPhrase}
          onChange={(event) => setTyped(event.target.value)}
        />
      </label>
      {passwordLabel !== undefined && (
        <form id={PASSWORD_FORM_ID} onSubmit={onSubmit}>
          {/* Visually hidden rather than type="hidden", which some managers
              ignore; out of the tab order and the accessibility tree. */}
          {username !== undefined && (
            <input
              type="text"
              name="username"
              autoComplete="username"
              value={username}
              readOnly
              tabIndex={-1}
              aria-hidden="true"
              className="visually-hidden"
            />
          )}
          <label className="field" htmlFor="confirm-dialog-password">
            {passwordLabel}
            <PasswordInput
              id="confirm-dialog-password"
              name="current-password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
        </form>
      )}
    </Modal>
  );
}
