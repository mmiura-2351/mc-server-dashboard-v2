import {
  createContext,
  type FocusEvent,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { t } from "../i18n/index.ts";
import { isFocusVisible } from "./focusVisible.ts";
import { setToastZone, topmostDialog } from "./Modal.tsx";

// Self-built toast primitive (WEBUI_SPEC.md Section 7.4 / 7.6 — no UI kit).
// API errors and success notices surface here; variants tint the left edge.

export type ToastVariant = "success" | "error";

interface Toast {
  id: number;
  message: string;
  variant: ToastVariant;
  // Times this error was raised while showing; a repeat collapses into it.
  count: number;
  // Order of the last raise, newest highest; the stack cap drops the lowest.
  raisedAt: number;
}

interface ToastContextValue {
  showToast: (message: string, variant: ToastVariant) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

// WEBUI_SPEC.md Section 7.4: an error toast stays until it is dismissed; any
// other closes after 2 s plus 100 ms per character, clamped to 3.2-12 s.
// Characters, not words, since Japanese has no word breaks.
const AUTO_DISMISS_BASE_MS = 2000;
const AUTO_DISMISS_PER_CHAR_MS = 100;
const AUTO_DISMISS_MIN_MS = 3200;
const AUTO_DISMISS_MAX_MS = 12000;

// WEBUI_SPEC.md Section 7.4: at most this many toasts of each kind show at
// once, so a timed toast never pushes out an error, nor an error a timed toast.
const MAX_ERROR_TOASTS = 3;
const MAX_TIMED_TOASTS = 2;

// `main` takes no focus on its own: it is made focusable just to take this
// focus, and stops being so once focus leaves it, so a later click in the page
// does not land on it.
function focusMain() {
  const main = document.querySelector("main");
  if (main === null) {
    return;
  }
  main.tabIndex = -1;
  main.addEventListener("blur", () => main.removeAttribute("tabindex"), {
    once: true,
  });
  main.focus();
}

function autoDismissMs(message: string): number {
  const scaled =
    AUTO_DISMISS_BASE_MS + AUTO_DISMISS_PER_CHAR_MS * message.length;
  return Math.min(AUTO_DISMISS_MAX_MS, Math.max(AUTO_DISMISS_MIN_MS, scaled));
}

// Makes room for one more toast of `variant`'s kind: once that kind is full,
// its oldest toast goes. The other kind is never touched.
function makeRoom(toasts: Toast[], variant: ToastVariant): Toast[] {
  const isError = variant === "error";
  const kind = toasts.filter(
    (toast) => (toast.variant === "error") === isError,
  );
  if (kind.length < (isError ? MAX_ERROR_TOASTS : MAX_TIMED_TOASTS)) {
    return toasts;
  }
  const oldest = kind.reduce((a, b) => (b.raisedAt < a.raisedAt ? b : a));
  return toasts.filter((toast) => toast !== oldest);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const raisesRef = useRef(0);
  const zoneRef = useRef<HTMLDivElement>(null);
  // The pointer or keyboard focus anywhere in the zone pauses every countdown,
  // so no toast closes, or slides, out from under the one being read.
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  // The element focus came into the stack from, to hand it back to.
  const returnFocusRef = useRef<HTMLElement | null>(null);

  // An open modal's focus trap runs on through the stack (WEBUI_SPEC.md 7.4).
  useLayoutEffect(() => {
    setToastZone(zoneRef.current);
    return () => setToastZone(null);
  }, []);

  // WEBUI_SPEC.md 7.4: a toast removed while it holds focus, dismissed, timed
  // out or dropped by the cap, hands focus on to the next toast, else the one
  // before, else back where focus came from, else to the open modal or `main`.
  // Never to the page body.
  const handOffFocus = useCallback((node: HTMLElement) => {
    const neighbour = (
      node.nextElementSibling ?? node.previousElementSibling
    )?.querySelector("button");
    if (neighbour) {
      neighbour.focus();
      return;
    }
    // Focus leaves the stack here, so where it came from is used up. It is
    // never taken back behind an open modal.
    const returnTo = returnFocusRef.current;
    returnFocusRef.current = null;
    const dialog = topmostDialog();
    const back = dialog === null || dialog.contains(returnTo) ? returnTo : null;
    for (const target of [back, dialog]) {
      if (target) {
        target.focus();
        if (document.activeElement === target) {
          return;
        }
      }
    }
    focusMain();
  }, []);

  // A toast removed while it holds focus or sits under the pointer takes them
  // out of the zone without a blur or mouseleave reaching it. Re-read focus
  // whenever the stack changes, and drop hover once the zone is empty.
  useEffect(() => {
    setFocused(keyboardFocusIn(zoneRef.current));
    if (toasts.length === 0) {
      setHovered(false);
    }
  }, [toasts]);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((toast) => toast.id !== id));
  }, []);

  const showToast = useCallback((message: string, variant: ToastVariant) => {
    const id = Date.now() + Math.random();
    raisesRef.current += 1;
    const raisedAt = raisesRef.current;
    setToasts((prev) => {
      const same =
        variant === "error"
          ? prev.find(
              (toast) => toast.variant === "error" && toast.message === message,
            )
          : undefined;
      if (same !== undefined) {
        // Updated in place, not moved, so a focused dismiss button keeps focus.
        return prev.map((toast) =>
          toast === same
            ? { ...toast, count: toast.count + 1, raisedAt }
            : toast,
        );
      }
      return [
        ...makeRoom(prev, variant),
        { id, message, variant, count: 1, raisedAt },
      ];
    });
  }, []);

  const value = useMemo<ToastContextValue>(() => ({ showToast }), [showToast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      {/* biome-ignore lint/a11y/noStaticElementInteractions: the pointer and focus here only pause the countdowns; each toast's dismiss button is the control. */}
      <div
        ref={zoneRef}
        className="toast-zone"
        // Pointer, not mouse, events: a tap fires mouseover with no mouseout
        // to follow, which left the stack paused; a touch pointer leaves as
        // the finger lifts.
        onPointerEnter={() => setHovered(true)}
        onPointerLeave={() => setHovered(false)}
        onFocus={(event) => {
          setFocused(keyboardFocusIn(event.currentTarget));
          if (relatedIsOutside(event)) {
            returnFocusRef.current = event.relatedTarget;
          }
        }}
        // Focus gone to another element has left the stack, so a later visit
        // that comes from nothing, as after a click on the page, returns to
        // the fallback. Gone to nothing, as the window loses focus, it has not.
        onBlur={(event) => {
          setFocused(false);
          if (relatedIsOutside(event)) {
            returnFocusRef.current = null;
          }
        }}
      >
        {toasts.map((toast) => (
          <ToastItem
            key={toast.id}
            toast={toast}
            paused={hovered || focused}
            onDismiss={dismiss}
            onRemoveFocused={handOffFocus}
          />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

// Whether keyboard focus is in `zone`. The focus a click or a tap leaves on a
// button shows no ring, and holds neither the countdowns nor the scroll
// (WEBUI_SPEC.md 7.4).
function keyboardFocusIn(zone: Element | null): boolean {
  const active = document.activeElement;
  return (
    active !== null && zone?.contains(active) === true && isFocusVisible(active)
  );
}

// Whether the other end of a focus move within the zone is an element outside
// it: where focus came from on focus, where it went on blur.
function relatedIsOutside(
  event: FocusEvent<HTMLElement>,
): event is FocusEvent<HTMLElement> & { relatedTarget: HTMLElement } {
  return (
    event.relatedTarget instanceof HTMLElement &&
    !event.currentTarget.contains(event.relatedTarget)
  );
}

// One toast. A timed toast counts down only while not `paused`, and resumes
// with the time left. The role alone makes it a live region: an explicit
// aria-live on top double-announces in VoiceOver.
function ToastItem({
  toast,
  paused,
  onDismiss,
  onRemoveFocused,
}: {
  toast: Toast;
  paused: boolean;
  onDismiss: (id: number) => void;
  onRemoveFocused: (node: HTMLElement) => void;
}) {
  const isError = toast.variant === "error";
  const remainingRef = useRef(autoDismissMs(toast.message));
  const ref = useRef<HTMLDivElement>(null);
  const messageId = useId();
  // Read at removal, so a new callback never re-runs the cleanup below on a
  // toast that is still showing.
  const onRemoveFocusedRef = useRef(onRemoveFocused);
  onRemoveFocusedRef.current = onRemoveFocused;

  // A layout effect's cleanup runs while the node is still in the document,
  // so focus is handed on before the browser would drop it to the body. It
  // runs once, at removal.
  useLayoutEffect(() => {
    const node = ref.current;
    return () => {
      if (node?.contains(document.activeElement)) {
        onRemoveFocusedRef.current(node);
      }
    };
  }, []);

  // A toast raised, or raised again, comes into view in case the stack
  // overflows and scrolls; not while keyboard focus is in the stack, so a
  // focused toast is not scrolled away.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `toast.raisedAt` marks a raise; re-run on each one, and only then.
  useEffect(() => {
    const node = ref.current;
    if (node !== null && !keyboardFocusIn(node.parentElement)) {
      node.scrollIntoView({ block: "nearest" });
    }
  }, [toast.raisedAt]);

  useEffect(() => {
    if (isError || paused) {
      return;
    }
    const startedAt = performance.now();
    const timer = setTimeout(() => onDismiss(toast.id), remainingRef.current);
    return () => {
      clearTimeout(timer);
      remainingRef.current -= performance.now() - startedAt;
    };
  }, [isError, paused, toast.id, onDismiss]);

  return (
    <div
      ref={ref}
      role={isError ? "alert" : "status"}
      className={`toast ${toast.variant}`}
    >
      <span id={messageId} className="toast-message">
        {toast.message}
      </span>
      {toast.count > 1 && (
        <span className="toast-count">
          <span aria-hidden="true">{toast.count}</span>
          <span className="visually-hidden">
            {t("common.toastRepeated", { count: toast.count })}
          </span>
        </span>
      )}
      <button
        type="button"
        className="toast-dismiss"
        aria-label={t("common.close")}
        aria-describedby={messageId}
        onClick={() => onDismiss(toast.id)}
        onKeyDown={(event) => {
          // Stop here: Modal and Popover close on an Escape at the document.
          if (event.key === "Escape") {
            event.stopPropagation();
            onDismiss(toast.id);
          }
        }}
      >
        ×
      </button>
    </div>
  );
}

export function useToast(): ToastContextValue {
  const value = useContext(ToastContext);
  if (value === null) {
    throw new Error("useToast must be used within a ToastProvider");
  }
  return value;
}
