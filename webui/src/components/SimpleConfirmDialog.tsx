import { t } from "../i18n/index.ts";
import { Modal } from "./Modal.tsx";

// Lightweight confirmation dialog for low-risk destructive actions. Unlike
// ConfirmDialog (typed-confirm), this shows a warning and confirm/cancel
// buttons without requiring the user to type a phrase.

interface SimpleConfirmDialogProps {
  open: boolean;
  title: string;
  body: string;
  confirmLabel: string;
  // Disables the confirm button while the triggered action is in flight, so a
  // double-click can't fire the mutation twice (#1591). Call sites that close
  // the dialog synchronously before mutating pass nothing.
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}

export function SimpleConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  busy,
  onConfirm,
  onClose,
}: SimpleConfirmDialogProps) {
  return (
    <Modal
      open={open}
      title={title}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="btn danger"
            disabled={busy}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </>
      }
    >
      <p>{body}</p>
    </Modal>
  );
}
