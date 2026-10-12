import { useState } from "react";
import { t } from "../i18n/index.ts";
import { Modal } from "./Modal.tsx";
import { PasswordInput } from "./PasswordInput.tsx";

// Require an exact confirmation phrase; optional password input is verified by the API.

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
        <label className="field" htmlFor="confirm-dialog-password">
          {passwordLabel}
          <PasswordInput
            id="confirm-dialog-password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
      )}
    </Modal>
  );
}
