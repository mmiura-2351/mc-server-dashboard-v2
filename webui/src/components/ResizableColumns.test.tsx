import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ResizableTable } from "./ResizableColumns.tsx";

// Drives the shared column-resize mechanism (#520): a <colgroup>-backed table
// whose column widths follow drag handles on the <th> boundaries, clamp to a
// minimum, persist per-table in localStorage, and reset on double-click.

const STORAGE_KEY = "mcsd.colw.test-table";

function Harness({ storageKey = STORAGE_KEY }: { storageKey?: string }) {
  return (
    <ResizableTable storageKey={storageKey} className="data">
      <thead>
        <tr>
          <th>Name</th>
          <th>Id</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>a</td>
          <td>b</td>
          <td>c</td>
        </tr>
      </tbody>
    </ResizableTable>
  );
}

function cols(): HTMLTableColElement[] {
  return Array.from(document.querySelectorAll("col"));
}

function handles(): HTMLElement[] {
  return screen.getAllByTestId("col-resize-handle");
}

function dragHandle(index: number, deltaX: number) {
  const handle = handles()[index];
  fireEvent.pointerDown(handle, { clientX: 100, pointerId: 1, button: 0 });
  fireEvent.pointerMove(window, { clientX: 100 + deltaX, pointerId: 1 });
  fireEvent.pointerUp(window, { clientX: 100 + deltaX, pointerId: 1 });
}

describe("ResizableTable", () => {
  beforeEach(() => {
    localStorage.clear();
  });
  afterEach(() => {
    localStorage.clear();
  });

  it("renders one resize handle per column", () => {
    render(<Harness />);
    expect(handles()).toHaveLength(3);
  });

  it("renders a <colgroup> with one <col> per column", () => {
    render(<Harness />);
    expect(cols()).toHaveLength(3);
  });

  it("widens a column when its handle is dragged right", () => {
    render(<Harness />);
    const before = cols()[0].style.width;
    dragHandle(0, 60);
    const after = cols()[0].style.width;
    expect(after).not.toBe(before);
    expect(Number.parseInt(after, 10)).toBeGreaterThan(
      Number.parseInt(before || "0", 10),
    );
  });

  it("clamps a column to the minimum width when dragged far left", () => {
    render(<Harness />);
    dragHandle(0, -5000);
    expect(Number.parseInt(cols()[0].style.width, 10)).toBeGreaterThanOrEqual(
      48,
    );
  });

  it("persists widths to localStorage and restores them on remount", () => {
    const { unmount } = render(<Harness />);
    dragHandle(0, 80);
    const widened = cols()[0].style.width;

    expect(localStorage.getItem(STORAGE_KEY)).not.toBeNull();

    unmount();
    render(<Harness />);
    expect(cols()[0].style.width).toBe(widened);
  });

  it("clears the resize cursor class on pointercancel", () => {
    render(<Harness />);
    fireEvent.pointerDown(handles()[0], {
      clientX: 100,
      pointerId: 1,
      button: 0,
    });
    expect(document.body.classList.contains("col-resizing")).toBe(true);
    fireEvent.pointerCancel(window, { pointerId: 1 });
    expect(document.body.classList.contains("col-resizing")).toBe(false);
  });

  it("leaves no resize cursor class behind when unmounted mid-drag", () => {
    const { unmount } = render(<Harness />);
    fireEvent.pointerDown(handles()[0], {
      clientX: 100,
      pointerId: 1,
      button: 0,
    });
    expect(document.body.classList.contains("col-resizing")).toBe(true);
    unmount();
    expect(document.body.classList.contains("col-resizing")).toBe(false);
  });

  it("ignores non-numeric persisted widths", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ 0: "wide", 1: -10, 2: 120 }),
    );
    render(<Harness />);
    expect(cols()[0].style.width).toBe("");
    expect(cols()[1].style.width).toBe("");
    expect(cols()[2].style.width).toBe("120px");
  });

  it("renders exactly one header row regardless of body row count (#534)", () => {
    render(
      <ResizableTable storageKey={STORAGE_KEY} className="data">
        <thead>
          <tr>
            <th>Name</th>
            <th>Id</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>a</td>
            <td>b</td>
          </tr>
          <tr>
            <td>c</td>
            <td>d</td>
          </tr>
        </tbody>
      </ResizableTable>,
    );
    expect(document.querySelectorAll("thead")).toHaveLength(1);
    expect(document.querySelectorAll("thead tr")).toHaveLength(1);
    expect(document.querySelectorAll("thead th")).toHaveLength(2);
  });

  it("resets a column to auto width on double-click of its handle", () => {
    render(<Harness />);
    dragHandle(0, 80);
    expect(cols()[0].style.width).not.toBe("");

    fireEvent.doubleClick(handles()[0]);
    expect(cols()[0].style.width).toBe("");
    const stored = localStorage.getItem(STORAGE_KEY);
    expect(stored === null || !stored.includes('"0"')).toBe(true);
  });
});
