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

/** Resize handles support pointer input only and are hidden from assistive technology. */

const MIN_WIDTH = 48;

// Missing column indices retain automatic widths.
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
    const clean: Widths = {};
    for (const [key, value] of Object.entries(parsed)) {
      if (typeof value === "number" && Number.isFinite(value) && value > 0) {
        clean[Number(key)] = value;
      }
    }
    return clean;
  } catch {
    // Unavailable or corrupt storage falls back to automatic widths.
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
    // Width persistence is best-effort when storage is unavailable or full.
  }
}

interface ResizableTableProps {
  /** Unique localStorage key identifying this table's saved widths. */
  storageKey: string;
  className?: string;
  /** Standard table contents: a `<thead>` followed by `<tbody>`. */
  children: ReactNode;
}

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
        // Window-level pointer listeners track dragging outside the handle without pointer capture.
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
  // Keep drag teardown available on unmount so listeners and the body cursor cannot remain active.
  const endDragRef = useRef<(() => void) | null>(null);

  const childArray = Children.toArray(children);
  const thead = childArray.find(
    (c): c is ReactElement<{ children?: ReactNode }> =>
      isValidElement(c) && c.type === "thead",
  );
  // Reuse one Children.toArray result; repeated calls clone elements and break headerRow identity checks.
  const theadChildren = thead ? Children.toArray(thead.props.children) : [];
  const headerRow = theadChildren.find(
    (c): c is ReactElement<{ children?: ReactNode }> =>
      isValidElement(c) && c.type === "tr",
  );
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

  // Measure the rendered width at drag start to prevent an initial jump.
  const beginResize = useCallback(
    (index: number, startX: number) => {
      const cell = tableRef.current?.querySelectorAll("thead th, thead td")[
        index
      ] as HTMLElement | undefined;
      const startWidth = cell?.getBoundingClientRect().width ?? MIN_WIDTH;

      const onMove = (e: PointerEvent) => {
        setColumnWidth(index, startWidth + (e.clientX - startX));
      };
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

  useEffect(() => {
    return () => {
      endDragRef.current?.();
    };
  }, []);

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
                // Reuse the stable keys assigned by Children.toArray.
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
          ...theadChildren.filter((c) => c !== headerRow),
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
