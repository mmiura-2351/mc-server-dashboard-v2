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

function autoDismissMs(message: string): number {
  const scaled =
    AUTO_DISMISS_BASE_MS + AUTO_DISMISS_PER_CHAR_MS * message.length;
  return Math.min(AUTO_DISMISS_MAX_MS, Math.max(AUTO_DISMISS_MIN_MS, scaled));
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((toast) => toast.id !== id));
  }, []);

  const showToast = useCallback((message: string, variant: ToastVariant) => {
    const id = Date.now() + Math.random();
    setToasts((prev) => [...prev, { id, message, variant }]);
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
