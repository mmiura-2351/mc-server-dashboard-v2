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
//   rather than escaping to the page behind the backdrop. The toast stack
//   joins the cycle: it sits outside every dialog, but an error raised over
//   one must stay reachable (WEBUI_SPEC.md Section 7.4).
// - Stacked dialogs: a module-level stack tracks open dialogs so Escape closes
//   only the topmost one (the Files history drawer + rollback confirm is a live
//   stacked case).

// Module-level registry of open dialogs, ordered by open time. The last entry
// is the topmost; only it reacts to Escape and traps Tab. Each Modal instance
// keys its slot with its own dialog element, so unmount removes the right one
// regardless of order.
const dialogStack: HTMLElement[] = [];

// The topmost open dialog, or null when none is open.
export function topmostDialog(): HTMLElement | null {
  return dialogStack.at(-1) ?? null;
}

// The toast stack, registered by ToastProvider, for the trap to include.
let toastZone: HTMLElement | null = null;

export function setToastZone(zone: HTMLElement | null): void {
  toastZone = zone;
}

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
    const dialog = dialogRef.current;
    if (!open || dialog === null) {
      return;
    }
    dialogStack.push(dialog);
    const trigger = document.activeElement as HTMLElement | null;
    dialog.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      // Only the topmost dialog reacts to keys.
      if (dialogStack.at(-1) !== dialog) {
        return;
      }
      if (event.key === "Escape") {
        onCloseRef.current();
        return;
      }
      if (event.key === "Tab") {
        // Tab runs natively within the dialog and within the toast stack; at
        // the end of either it steps to the start of the other.
        const inDialog = focusableWithin(dialog);
        const inToasts = toastZone === null ? [] : focusableWithin(toastZone);
        const cycle = [...inDialog, ...inToasts];
        if (cycle.length === 0) {
          event.preventDefault();
          dialog.focus();
          return;
        }
        const active = document.activeElement;
        let next: HTMLElement | undefined;
        if (event.shiftKey) {
          if (active === dialog || active === cycle[0]) {
            next = cycle.at(-1);
          } else if (active === inToasts[0]) {
            next = inDialog.at(-1);
          }
        } else if (active === cycle.at(-1)) {
          next = cycle[0];
        } else if (active === (inDialog.at(-1) ?? dialog)) {
          next = inToasts[0];
        }
        if (next !== undefined) {
          event.preventDefault();
          next.focus();
        }
      }
    };
    document.addEventListener("keydown", onKeyDown);

    return () => {
      document.removeEventListener("keydown", onKeyDown);
      const slot = dialogStack.indexOf(dialog);
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
