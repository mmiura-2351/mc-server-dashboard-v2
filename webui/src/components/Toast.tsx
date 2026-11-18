import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { t } from "../i18n/index.ts";

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
  // The pointer or focus anywhere in the zone pauses every countdown, so no
  // toast closes, or slides, out from under the one being read.
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);

  // A toast removed while it holds focus or sits under the pointer takes them
  // out of the zone without a blur or mouseleave reaching it. Re-read focus
  // whenever the stack changes, and drop hover once the zone is empty.
  useEffect(() => {
    setFocused(zoneRef.current?.contains(document.activeElement) === true);
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
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
      >
        {toasts.map((toast) => (
          <ToastItem
            key={toast.id}
            toast={toast}
            paused={hovered || focused}
            onDismiss={dismiss}
          />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

// One toast. A timed toast counts down only while not `paused`, and resumes
// with the time left. The role alone makes it a live region: an explicit
// aria-live on top double-announces in VoiceOver.
function ToastItem({
  toast,
  paused,
  onDismiss,
}: {
  toast: Toast;
  paused: boolean;
  onDismiss: (id: number) => void;
}) {
  const isError = toast.variant === "error";
  const remainingRef = useRef(autoDismissMs(toast.message));
  const ref = useRef<HTMLDivElement>(null);

  // A toast raised, or raised again, comes into view in case the stack
  // overflows and scrolls; not while focus is in the stack, so a focused
  // toast is not scrolled away.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `toast.raisedAt` marks a raise; re-run on each one, and only then.
  useEffect(() => {
    const node = ref.current;
    if (
      node !== null &&
      !node.parentElement?.contains(document.activeElement)
    ) {
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
      <span className="toast-message">{toast.message}</span>
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
