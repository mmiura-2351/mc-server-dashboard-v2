import { type ReactNode, useEffect, useRef } from "react";
import { t } from "../i18n/index.ts";
import { isComposing } from "./isComposing.ts";

// Self-built modal primitive (WEBUI_SPEC.md Section 7.4 / 7.6 — no UI kit).
// The backdrop is a sibling button behind the dialog so clicking it closes the
// modal, while clicks inside the dialog never reach it. Escape is handled via a
// window-level listener while open, and focus moves into the dialog on mount.
//
// Accessibility hardening (#408):
// - Focus restore: the element focused when the dialog opened is refocused on
//   close, so keyboard users land back on the trigger.
// - Focus trap: Tab / Shift+Tab cycle within the dialog's focusable elements
//   rather than escaping to the page behind the backdrop; a scroller the
//   browser stops Tab on counts as one of them. The toast stack
//   joins the cycle: it sits outside every dialog, but an error raised over
//   one must stay reachable (WEBUI_SPEC.md Section 7.4). A Tab that lands
//   anywhere else is pulled back into the dialog, and a control disabled
//   while it holds focus hands it to the dialog itself.
// - Stacked dialogs: a module-level stack tracks open dialogs so Escape closes
//   only the topmost one (the Files history drawer + rollback confirm is a live
//   stacked case).
// - Layers inside a dialog: the window hears a key after every listener on the
//   document, so a popover inside the dialog takes Escape first and marks it
//   handled, and the dialog leaves a handled Escape alone (#3971).

// Module-level registry of open dialogs, ordered by open time. The last entry
// is the topmost; only it reacts to Escape and traps Tab. Each Modal instance
// keys its slot with its own dialog element, so unmount removes the right one
// regardless of order.
const dialogStack: HTMLElement[] = [];

// The topmost open dialog, or null when none is open. A document-level key
// handler of the page behind leaves keys to it while one is open (#3920).
export function topmostDialog(): HTMLElement | null {
  return dialogStack.at(-1) ?? null;
}

// The toast stack, registered by ToastProvider, for the trap to include.
let toastZone: HTMLElement | null = null;

export function setToastZone(zone: HTMLElement | null): void {
  toastZone = zone;
}

// `:disabled`, unlike `[disabled]`, also matches a control inside a disabled
// fieldset.
const CONTROLS =
  'a[href], button:not(:disabled), input:not(:disabled):not([type="hidden"]), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

// The controls under `root`, and the scrollers the browser also stops Tab on,
// in document order.
function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(
    root.querySelectorAll<HTMLElement>(`${CONTROLS}, .modal-body`),
  ).flatMap((el) => (el.matches(CONTROLS) ? [el] : tabbableScrollers(el)));
}

// Chromium makes a scroller a Tab stop while its content overflows on an axis
// the user can scroll and nothing in it takes Tab, as a `.modal-body` whose
// controls are all disabled. A scroller nested in it that qualifies takes the
// stop instead. Only a body with no control in it is searched, so a Tab in a
// long list of controls stays cheap.
function tabbableScrollers(body: HTMLElement): HTMLElement[] {
  if (body.querySelector(CONTROLS) !== null) {
    return [];
  }
  const scrolls = (overflow: string) =>
    overflow === "auto" || overflow === "scroll";
  const scrollers = [
    body,
    ...body.querySelectorAll<HTMLElement>("*:not([tabindex])"),
  ].filter((el) => {
    const style = getComputedStyle(el);
    return (
      (scrolls(style.overflowY) && el.scrollHeight > el.clientHeight) ||
      (scrolls(style.overflowX) && el.scrollWidth > el.clientWidth)
    );
  });
  return scrollers.filter(
    (outer) => !scrollers.some((el) => el !== outer && outer.contains(el)),
  );
}

// Focuses the first of `targets` that takes focus, else the dialog itself. A
// control can match the selector above and still refuse focus (one under
// `display: none`), and a Tab the trap has taken over must land somewhere.
function focusFirstOf(
  targets: HTMLElement[],
  dialog: HTMLElement,
  options?: FocusOptions,
): void {
  for (const target of [...targets, dialog]) {
    target.focus(options);
    if (document.activeElement === target) {
      return;
    }
  }
}

// The stops of `cycle` from the one a Tab from `active` goes to next, round
// to `active`, when that one is a scroller; else undefined. A browser that
// does not stop Tab on a scroller runs past it, as Chromium does past a
// disabled scrolling textarea, and may run out of the dialog.
function onwardFromScroller(
  cycle: HTMLElement[],
  active: Element,
  dialog: HTMLElement,
  back: boolean,
): HTMLElement[] | undefined {
  const at = cycle.indexOf(active as HTMLElement);
  const next = cycle[at + (back ? -1 : 1)];
  if (
    (at === -1 && active !== dialog) ||
    next === undefined ||
    next.matches(CONTROLS)
  ) {
    return undefined;
  }
  const ring = back ? [...cycle].reverse() : cycle;
  const from = ring.indexOf(next);
  return [...ring.slice(from), ...ring.slice(0, from)];
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
  // focus restore, move focus into the dialog, and listen on the window for
  // Escape (topmost only) and Tab (focus trap). Cleanup restores trigger focus.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!open || dialog === null) {
      return;
    }
    dialogStack.push(dialog);
    const trigger = document.activeElement as HTMLElement | null;
    dialog.focus();

    // A control disabled while it holds focus, as a confirm is while its
    // action runs, keeps focus on nothing usable until the browser drops it
    // to the page body (Chromium, a frame later). The dialog takes it first,
    // unscrolled. Only that loss moves focus, never a control disabled
    // elsewhere or enabled again, so focus moves at most once per confirm
    // rather than on every busy toggle, and the dialog is not meant to be
    // announced anew each time.
    const observer = new MutationObserver(() => {
      if (
        dialogStack.at(-1) === dialog &&
        document.activeElement?.matches(":disabled")
      ) {
        dialog.focus({ preventScroll: true });
      }
    });
    observer.observe(dialog, {
      attributes: true,
      attributeFilter: ["disabled"],
      subtree: true,
    });

    // The last Tab's direction and the page's scroll before it, and the
    // stops on from a scroller it goes to next, kept until the browser has
    // moved focus for it: used up by the first focus that follows, or
    // cleared at its keyup, or by a timer when that keyup is lost, as when
    // Tab leaves the page.
    let lastTab: {
      shiftKey: boolean;
      x: number;
      y: number;
      onward?: HTMLElement[];
    } | null = null;

    // A Tab that lands focus outside the dialog and the toast stack, the
    // browser's own from the page body or one that leaves the dialog, is
    // pulled back: to the dialog's first control, or its last after
    // Shift+Tab. One that ran past the scroller it went to next goes on from
    // that scroller instead, the toast stack included (#3971). The control
    // may scroll into view inside the dialog; the page's scroll the Tab made
    // is undone. Focus a script or the pointer moves is left alone, even
    // while the Tab's key is still down: a confirm may close the dialog and
    // focus behind it in any order, and the keydown branch below takes the
    // next Tab. Only the topmost dialog pulls: one that opens over it during
    // the Tab, from a blur handler say, keeps the focus it takes.
    const isOutside = (node: Node | null) =>
      !dialog.contains(node) && !toastZone?.contains(node);
    const onFocusIn = () => {
      const tab = lastTab;
      lastTab = null;
      if (
        dialogStack.at(-1) !== dialog ||
        tab === null ||
        !isOutside(document.activeElement)
      ) {
        return;
      }
      const inDialog = focusableWithin(dialog);
      focusFirstOf(
        tab.onward ?? (tab.shiftKey ? inDialog.reverse() : inDialog),
        dialog,
      );
      window.scrollTo(tab.x, tab.y);
    };
    document.addEventListener("focusin", onFocusIn);

    const onKeyDown = (event: KeyboardEvent) => {
      // Only the topmost dialog reacts to keys.
      if (dialogStack.at(-1) !== dialog) {
        return;
      }
      if (event.key === "Escape") {
        // An inner layer, as a popover in the dialog, has taken it (#3971),
        // or the IME has, to cancel a conversion in a field (#3993).
        if (event.defaultPrevented || isComposing(event)) {
          return;
        }
        // Handled, so a listener that hears the Escape after this one leaves
        // it alone (#3920).
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key === "Tab") {
        lastTab = {
          shiftKey: event.shiftKey,
          x: window.scrollX,
          y: window.scrollY,
        };
        setTimeout(() => {
          lastTab = null;
        });
        const active = document.activeElement;
        // From the page body the browser runs Tab: after a focused control is
        // removed it goes on from where that control was. If it lands
        // outside, it is pulled back above.
        if (active === null || active === document.body) {
          return;
        }
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
        // Where Tab goes, best first.
        let order: HTMLElement[] | undefined;
        if (isOutside(active)) {
          // From outside both, on the page behind or in a dialog below, Tab
          // enters the dialog at its start or end.
          order = event.shiftKey ? [...inDialog].reverse() : inDialog;
        } else if (event.shiftKey) {
          if (active === dialog || active === cycle[0]) {
            order = [...cycle].reverse();
          } else if (active === inToasts[0]) {
            order = [...inDialog].reverse();
          }
        } else if (active === cycle.at(-1)) {
          order = cycle;
        } else if (active === (inDialog.at(-1) ?? dialog)) {
          order = inToasts;
        }
        if (order !== undefined) {
          event.preventDefault();
          focusFirstOf(order, dialog);
        } else {
          lastTab.onward = onwardFromScroller(
            cycle,
            active,
            dialog,
            event.shiftKey,
          );
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.key === "Tab") {
        lastTab = null;
      }
    };
    document.addEventListener("keyup", onKeyUp);

    return () => {
      observer.disconnect();
      document.removeEventListener("focusin", onFocusIn);
      window.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("keyup", onKeyUp);
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
