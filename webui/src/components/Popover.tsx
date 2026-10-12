import {
  type ReactNode,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

// Keep the panel beside the trigger in DOM order so native controls remain keyboard reachable.

const VIEWPORT_MARGIN = 8;

/** Clamp the trigger-relative offset to viewport margins; oversized panels align to the left margin. */
export function clampPopoverOffset(
  triggerLeft: number,
  panelWidth: number,
  viewportWidth: number,
  margin: number,
): number {
  const maxLeft = Math.max(margin, viewportWidth - margin - panelWidth);
  const targetLeft = Math.min(Math.max(triggerLeft, margin), maxLeft);
  return targetLeft - triggerLeft;
}

interface PopoverProps {
  label: ReactNode;
  /** Stable accessible name for the trigger when `label` is decorative. */
  buttonAriaLabel?: string;
  buttonClassName?: string;
  panelClassName?: string;
  /** Function children receive close so panel actions can dismiss the popover. */
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
  const [offset, setOffset] = useState(0);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();

  // Measure before paint to avoid a positioning flash; recompute while open on resize.
  useLayoutEffect(() => {
    if (!open) {
      return;
    }
    const reposition = () => {
      const wrapper = wrapperRef.current;
      const panel = panelRef.current;
      if (!wrapper || !panel) {
        return;
      }
      setOffset(
        clampPopoverOffset(
          wrapper.getBoundingClientRect().left,
          panel.offsetWidth,
          window.innerWidth,
          VIEWPORT_MARGIN,
        ),
      );
    };
    reposition();
    window.addEventListener("resize", reposition);
    return () => window.removeEventListener("resize", reposition);
  }, [open]);

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
        // No aria-haspopup: this panel contains ordinary controls, rather than a menu or listbox.
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
          ref={panelRef}
          className={
            panelClassName ? `popover-panel ${panelClassName}` : "popover-panel"
          }
          style={{ left: offset }}
        >
          {typeof children === "function" ? children(close) : children}
        </div>
      )}
    </div>
  );
}
