import { type ReactNode, useEffect, useRef } from "react";
import { t } from "../i18n/index.ts";

// Only the last open dialog handles Escape and traps Tab; symbols allow out-of-order removal.
const dialogStack: symbol[] = [];

function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  );
}

interface ModalProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}

export function Modal({ open, title, onClose, children, footer }: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  // Keep onClose current without rerunning the focus effect on every render.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) {
      return;
    }
    const id = Symbol("modal");
    dialogStack.push(id);
    const trigger = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (dialogStack.at(-1) !== id) {
        return;
      }
      if (event.key === "Escape") {
        onCloseRef.current();
        return;
      }
      if (event.key === "Tab") {
        const dialog = dialogRef.current;
        if (dialog === null) {
          return;
        }
        const focusable = focusableWithin(dialog);
        if (focusable.length === 0) {
          event.preventDefault();
          dialog.focus();
          return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        const active = document.activeElement;
        if (event.shiftKey && (active === first || active === dialog)) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && active === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKeyDown);

    return () => {
      document.removeEventListener("keydown", onKeyDown);
      const slot = dialogStack.indexOf(id);
      if (slot !== -1) {
        dialogStack.splice(slot, 1);
      }
      trigger?.focus();
    };
  }, [open]);

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
