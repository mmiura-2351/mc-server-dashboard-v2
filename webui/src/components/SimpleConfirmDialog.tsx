import { t } from "../i18n/index.ts";
import { Modal } from "./Modal.tsx";

interface SimpleConfirmDialogProps {
  open: boolean;
  title: string;
  body: string;
  confirmLabel: string;
  // Disable confirmation while the action runs to prevent duplicate submissions.
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
