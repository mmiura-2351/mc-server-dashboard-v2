import {
  Children,
  cloneElement,
  isValidElement,
  type ReactElement,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { t } from "../i18n/index.ts";

/**
 * Shared user-resizable table columns (#520). Owner UX feedback: fixed column
 * widths fight long ids/names on one side and narrow screens on the other.
 *
 * Mechanism: the table is rendered `table-layout: fixed` with a generated
 * `<colgroup>`; each header cell gets a drag handle on its right boundary. A
 * pointer drag adjusts that column's `<col>` width (clamped to a minimum);
 * double-click resets the column to auto. Widths persist per table in
 * localStorage under the caller's `storageKey`, so they survive reloads.
 *
 * No new dependency — plain pointer events with capture + window-level
 * listeners that clean up on release.
 *
 * A11y: the handles are a pointer-only affordance (`title` tooltip), hidden
 * from assistive tech because they offer no keyboard path — matching the
 * hover-only posture elsewhere in the webui (the audit table's title-on-hover,
 * #519). Keyboard-driven resize is intentionally out of scope here and tracked
 * as a #496-class gap.
 */

// Smallest a column may be dragged to (px). Keeps a column from collapsing to
// an unusable sliver.
const MIN_WIDTH = 48;

// Per-column width overrides, keyed by column index. A missing entry means the
// column keeps its natural (auto) width.
type Widths = Record<number, number>;

function loadWidths(storageKey: string): Widths {
  try {
    const raw = localStorage.getItem(storageKey);
    if (raw === null) {
      return {};
    }
    const parsed = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) {
      return {};
    }
    // Keep only finite positive widths; a hand-edited or corrupt entry must not
    // feed a garbage `width` into the <col> style.
    const clean: Widths = {};
    for (const [key, value] of Object.entries(parsed)) {
      if (typeof value === "number" && Number.isFinite(value) && value > 0) {
        clean[Number(key)] = value;
      }
    }
    return clean;
  } catch {
    // Corrupt/blocked storage must never break the table; fall back to auto.
    return {};
  }
}

function saveWidths(storageKey: string, widths: Widths): void {
  try {
    if (Object.keys(widths).length === 0) {
      localStorage.removeItem(storageKey);
    } else {
      localStorage.setItem(storageKey, JSON.stringify(widths));
    }
  } catch {
    // Best-effort persistence; ignore quota/availability failures.
  }
}

interface ResizableTableProps {
  /** Unique localStorage key identifying this table's saved widths. */
  storageKey: string;
  className?: string;
  /** Standard table contents: a `<thead>` followed by `<tbody>`. */
  children: ReactNode;
}

// A drag handle injected on each header cell's right boundary.
function ResizeHandle({
  onResizeStart,
  onReset,
}: {
  onResizeStart: (clientX: number) => void;
  onReset: () => void;
}) {
  return (
    <span
      className="col-resize-handle"
      data-testid="col-resize-handle"
      aria-hidden="true"
      title={t("common.resizeColumn")}
      onPointerDown={(e) => {
        if (e.button !== 0) {
          return;
        }
        // The drag tracks via window-level pointermove/pointerup listeners
        // (see beginResize), so the handle does not need to capture the
        // pointer — those listeners fire wherever the cursor goes.
        e.preventDefault();
        onResizeStart(e.clientX);
      }}
      onDoubleClick={onReset}
    />
  );
}

export function ResizableTable({
  storageKey,
  className,
  children,
}: ResizableTableProps) {
  const tableRef = useRef<HTMLTableElement>(null);
  const [widths, setWidths] = useState<Widths>(() => loadWidths(storageKey));
  // Teardown for the drag in progress, if any. Held in a ref so unmount
  // cleanup can run it — pagination/state changes mid-drag can unmount the
  // table while window listeners and the body cursor class are still live.
  const endDragRef = useRef<(() => void) | null>(null);

  // The header cells are the first <tr> of the <thead> child; their count is
  // the column count and drives the <colgroup>.
  const childArray = Children.toArray(children);
  const thead = childArray.find(
    (c): c is ReactElement<{ children?: ReactNode }> =>
      isValidElement(c) && c.type === "thead",
  );
  const headerRow = thead
    ? Children.toArray(thead.props.children).find(
        (c): c is ReactElement<{ children?: ReactNode }> =>
          isValidElement(c) && c.type === "tr",
      )
    : undefined;
  const headerCells = headerRow
    ? Children.toArray(headerRow.props.children).filter(isValidElement)
    : [];
  const columnCount = headerCells.length;

  const setColumnWidth = useCallback(
    (index: number, width: number) => {
      setWidths((prev) => {
        const next = {
          ...prev,
          [index]: Math.max(MIN_WIDTH, Math.round(width)),
        };
        saveWidths(storageKey, next);
        return next;
      });
    },
    [storageKey],
  );

  const resetColumn = useCallback(
    (index: number) => {
      setWidths((prev) => {
        if (!(index in prev)) {
          return prev;
        }
        const next = { ...prev };
        delete next[index];
        saveWidths(storageKey, next);
        return next;
      });
    },
    [storageKey],
  );

  // Begin a drag from `startX`, measuring the column's current rendered width
  // as the baseline so the first pixel of movement does not jump.
  const beginResize = useCallback(
    (index: number, startX: number) => {
      const cell = tableRef.current?.querySelectorAll("thead th, thead td")[
        index
      ] as HTMLElement | undefined;
      const startWidth = cell?.getBoundingClientRect().width ?? MIN_WIDTH;

      const onMove = (e: PointerEvent) => {
        setColumnWidth(index, startWidth + (e.clientX - startX));
      };
      // Shared teardown for a normal release (pointerup), a cancelled drag
      // (pointercancel — e.g. the browser hijacks the pointer), and unmount.
      const endDrag = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", endDrag);
        window.removeEventListener("pointercancel", endDrag);
        document.body.classList.remove("col-resizing");
        endDragRef.current = null;
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", endDrag);
      window.addEventListener("pointercancel", endDrag);
      document.body.classList.add("col-resizing");
      endDragRef.current = endDrag;
    },
    [setColumnWidth],
  );

  // If the table unmounts mid-drag (plausible when pagination/state changes
  // swap it out), tear down the live window listeners and body cursor class so
  // nothing is left stuck.
  useEffect(() => {
    return () => {
      endDragRef.current?.();
    };
  }, []);

  // Re-render the header row with a resize handle appended to each cell.
  const decoratedThead =
    thead && headerRow
      ? cloneElement(thead, undefined, [
          cloneElement(
            headerRow,
            { key: "header-row" },
            headerCells.map((cell, index) => {
              const headerCell = cell as ReactElement<{
                children?: ReactNode;
              }>;
              return cloneElement(
                headerCell,
                // Children.toArray already assigned each cell a stable key
                // ("…0", "…1"); reuse it rather than re-keying by index.
                { key: headerCell.key },
                <>
                  {headerCell.props.children}
                  <ResizeHandle
                    onResizeStart={(clientX) => beginResize(index, clientX)}
                    onReset={() => resetColumn(index)}
                  />
                </>,
              );
            }),
          ),
          ...Children.toArray(thead.props.children).filter(
            (c) => c !== headerRow,
          ),
        ])
      : thead;

  const rest = childArray.filter((c) => c !== thead);

  return (
    <table
      ref={tableRef}
      className={className}
      style={{ tableLayout: "fixed" }}
    >
      <colgroup>
        {Array.from({ length: columnCount }, (_, i) => (
          <col
            // biome-ignore lint/suspicious/noArrayIndexKey: columns are positional
            key={`col-${i}`}
            style={i in widths ? { width: `${widths[i]}px` } : undefined}
          />
        ))}
      </colgroup>
      {decoratedThead}
      {rest}
    </table>
  );
}
