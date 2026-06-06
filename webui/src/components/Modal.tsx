import { type ReactNode, useEffect, useRef } from "react";
import { t } from "../i18n/index.ts";

// Self-built modal primitive (WEBUI_SPEC.md Section 7.4 / 7.6 — no UI kit).
// The backdrop is a sibling button behind the dialog so clicking it closes the
// modal, while clicks inside the dialog never reach it. Escape is handled via a
// document-level listener while open, and focus moves into the dialog on mount.
//
// Accessibility hardening (#408):
// - Focus restore: the element focused when the dialog opened is refocused on
//   close, so keyboard users land back on the trigger.
// - Focus trap: Tab / Shift+Tab cycle within the dialog's focusable elements
//   rather than escaping to the page behind the backdrop.
// - Stacked dialogs: a module-level stack tracks open dialogs so Escape closes
//   only the topmost one (the Files history drawer + rollback confirm is a live
//   stacked case).

// Module-level registry of open dialogs, ordered by open time. The last entry
// is the topmost; only it reacts to Escape and traps Tab. A symbol per Modal
// instance keys its slot so unmount removes the right one regardless of order.
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
  // Hold the latest onClose so the once-per-open effect's keydown handler never
  // goes stale, without re-running the effect (and stealing focus back into the
  // dialog) on every render — callers pass a fresh onClose each render.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Runs once per open → close: register on the stack, capture the trigger for
  // focus restore, move focus into the dialog, and listen on the document for
  // Escape (topmost only) and Tab (focus trap). Cleanup restores trigger focus.
  useEffect(() => {
    if (!open) {
      return;
    }
    const id = Symbol("modal");
    dialogStack.push(id);
    const trigger = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      // Only the topmost dialog reacts to keys.
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
