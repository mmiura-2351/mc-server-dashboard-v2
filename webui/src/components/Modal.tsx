import { type ReactNode, useEffect, useRef } from "react";
import { t } from "../i18n/index.ts";

// Self-built modal primitive (WEBUI_SPEC.md Section 7.4 / 7.6 — no UI kit).
// The backdrop is a sibling button behind the dialog so clicking it closes the
// modal, while clicks inside the dialog never reach it. Escape is handled via a
// document-level listener while open, and focus moves into the dialog on mount.

interface ModalProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}

export function Modal({ open, title, onClose, children, footer }: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);

  // While open, listen for Escape on the document so it closes regardless of
  // focus, and move focus into the dialog on mount for accessibility.
  useEffect(() => {
    if (!open) {
      return;
    }
    dialogRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) {
    return null;
  }

  return (
    <div className="modal-layer">
      <button
        type="button"
        className="modal-backdrop"
        data-testid="modal-backdrop"
        aria-label={t("common.close")}
        onClick={onClose}
      />
      <div
        ref={dialogRef}
        className="card modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
      >
        <h2>{title}</h2>
        <div className="modal-body">{children}</div>
        {footer !== undefined && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}
