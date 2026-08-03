import { type ReactNode, useEffect, useId, useRef, useState } from "react";

// Self-built popover primitive (WEBUI_SPEC.md 7.6 — no UI kit): a trigger
// button plus an absolutely-positioned panel that closes on outside-click and
// Escape. The panel is a sibling of the trigger inside the wrapper, so keyboard
// focus tabs straight from the trigger into it and native controls inside stay
// reachable/toggleable by keyboard. Escape restores focus to the trigger.

interface PopoverProps {
  /** Visible content of the trigger button (may include badges/carets). */
  label: ReactNode;
  /** Stable accessible name for the trigger when `label` is decorative. */
  buttonAriaLabel?: string;
  buttonClassName?: string;
  panelClassName?: string;
  /**
   * Panel content. A function form receives a `close` callback so an action
   * inside the panel can dismiss it.
   */
  children: ReactNode | ((close: () => void) => ReactNode);
}

export function Popover({
  label,
  buttonAriaLabel,
  buttonClassName,
  panelClassName,
  children,
}: PopoverProps) {
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  // While open, dismiss on an outside pointer-down or Escape. Escape also
  // returns focus to the trigger so keyboard users land back where they were.
  useEffect(() => {
    if (!open) {
      return;
    }
    const onPointerDown = (event: MouseEvent) => {
      if (!wrapperRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const close = () => setOpen(false);

  return (
    <div className="popover" ref={wrapperRef}>
      <button
        type="button"
        ref={triggerRef}
        className={buttonClassName}
        aria-haspopup="true"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-label={buttonAriaLabel}
        onClick={() => setOpen((prev) => !prev)}
      >
        {label}
      </button>
      {open && (
        <div
          id={panelId}
          className={
            panelClassName ? `popover-panel ${panelClassName}` : "popover-panel"
          }
        >
          {typeof children === "function" ? children(close) : children}
        </div>
      )}
    </div>
  );
}
