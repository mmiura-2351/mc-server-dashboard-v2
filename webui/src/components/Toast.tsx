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

// WEBUI_SPEC.md Section 7.4: at most this many toasts show at once.
const MAX_TOASTS = 5;

function autoDismissMs(message: string): number {
  const scaled =
    AUTO_DISMISS_BASE_MS + AUTO_DISMISS_PER_CHAR_MS * message.length;
  return Math.min(AUTO_DISMISS_MAX_MS, Math.max(AUTO_DISMISS_MIN_MS, scaled));
}

// Makes room for one more toast: the oldest timed toast goes first, and the
// oldest error only once errors are all that is showing.
function dropOldest(toasts: Toast[]): Toast[] {
  const timed = toasts.filter((toast) => toast.variant !== "error");
  const pool = timed.length > 0 ? timed : toasts;
  const oldest = pool.reduce((a, b) => (b.raisedAt < a.raisedAt ? b : a));
  return toasts.filter((toast) => toast !== oldest);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const raisesRef = useRef(0);

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
      const kept = prev.length < MAX_TOASTS ? prev : dropOldest(prev);
      return [...kept, { id, message, variant, count: 1, raisedAt }];
    });
  }, []);

  const value = useMemo<ToastContextValue>(() => ({ showToast }), [showToast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-zone">
        {toasts.map((toast) => (
          <ToastItem key={toast.id} toast={toast} onDismiss={dismiss} />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

// One toast. Hovering it or focusing its dismiss button pauses the countdown,
// which resumes with the time left once neither holds.
function ToastItem({
  toast,
  onDismiss,
}: {
  toast: Toast;
  onDismiss: (id: number) => void;
}) {
  const isError = toast.variant === "error";
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const remainingRef = useRef(autoDismissMs(toast.message));
  const paused = hovered || focused;

  useEffect(() => {
    if (isError || paused) {
      return;
    }
    const startedAt = Date.now();
    const timer = setTimeout(() => onDismiss(toast.id), remainingRef.current);
    return () => {
      clearTimeout(timer);
      remainingRef.current -= Date.now() - startedAt;
    };
  }, [isError, paused, toast.id, onDismiss]);

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: the role is alert or status per variant, which the rule cannot read off an expression; hover only pauses the countdown, as focusing the dismiss button does.
    <div
      role={isError ? "alert" : "status"}
      aria-live={isError ? "assertive" : "polite"}
      className={`toast ${toast.variant}`}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <span className="toast-message">{toast.message}</span>
      {toast.count > 1 && <span className="toast-count">×{toast.count}</span>}
      <button
        type="button"
        className="toast-dismiss"
        aria-label={t("common.close")}
        onClick={() => onDismiss(toast.id)}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
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
