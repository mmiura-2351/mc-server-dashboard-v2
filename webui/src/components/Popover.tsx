import {
  type ReactNode,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

// Self-built popover primitive (WEBUI_SPEC.md 7.6 — no UI kit): a trigger
// button plus an absolutely-positioned panel that closes on outside-click and
// Escape. The panel is a sibling of the trigger inside the wrapper, so keyboard
// focus tabs straight from the trigger into it and native controls inside stay
// reachable/toggleable by keyboard. Escape restores focus to the trigger.

// Gap kept between the panel and the viewport edges (px).
const VIEWPORT_MARGIN = 8;

/**
 * Horizontal offset (px, relative to the trigger's left edge) that keeps an
 * open panel within the viewport at both edges. The panel renders left-aligned
 * to the trigger (`triggerLeft` is its un-shifted viewport x); this shifts it
 * left when it would overflow the right edge, but never past the left margin,
 * and degrades to a left-margin alignment when the panel is wider than the
 * available width (#2239 narrow-viewport overflow).
 */
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
  const [offset, setOffset] = useState(0);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();

  // Once the panel is laid out, shift it horizontally so it never overflows the
  // viewport at either edge — measured before paint (no flash), and recomputed
  // on resize while open. The panel renders left-aligned to the trigger, so the
  // clamp works from the wrapper's viewport x (#2239).
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
        // No aria-haspopup: the panel is a plain content region (a checkbox
        // group in the dashboard filter's use), not a menu/listbox, so the
        // disclosure leans on aria-expanded + aria-controls instead (#2668).
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
