import { useState } from "react";
import { t } from "../i18n/index.ts";
import { Modal } from "./Modal.tsx";

// Typed-confirm dialog for destructive operations (WEBUI_SPEC.md Section 7.4).
// The destructive button stays disabled until the user types confirmPhrase
// exactly (case-sensitive), guarding deletes and other irreversible actions.
//
// When passwordLabel is set, the dialog also re-authenticates: a password field
// is rendered and the button additionally requires a non-empty password, which
// is passed to onConfirm. Account deletion uses this (WEBUI_SPEC.md 6.11); the
// other destructive flows omit it and the field never renders.

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  body: string;
  confirmPhrase: string;
  confirmLabel: string;
  promptLabel: string;
  passwordLabel?: string;
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
          <button
            type="button"
            className="btn danger"
            disabled={!matches}
            onClick={confirm}
          >
            {confirmLabel}
          </button>
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
        <label className="field">
          {passwordLabel}
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
      )}
    </Modal>
  );
}
