import { useState } from "react";
import { t } from "../i18n/index.ts";
import { Modal } from "./Modal.tsx";

// Typed-confirm dialog for destructive operations (WEBUI_SPEC.md Section 7.4).
// The destructive button stays disabled until the user types confirmPhrase
// exactly (case-sensitive), guarding deletes and other irreversible actions.

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  body: string;
  confirmPhrase: string;
  confirmLabel: string;
  promptLabel: string;
  onConfirm: () => void;
  onClose: () => void;
}

export function ConfirmDialog({
  open,
  title,
  body,
  confirmPhrase,
  confirmLabel,
  promptLabel,
  onConfirm,
  onClose,
}: ConfirmDialogProps) {
  const [typed, setTyped] = useState("");
  const matches = typed === confirmPhrase;

  const close = () => {
    setTyped("");
    onClose();
  };

  const confirm = () => {
    setTyped("");
    onConfirm();
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
    </Modal>
  );
}
