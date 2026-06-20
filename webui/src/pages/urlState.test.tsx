/**
 * Unit tests for the URL-driven view-state hooks (#514): the active tab lives in
 * the hash, the page offset in `?offset=N`, both drive history so Back restores
 * the prior view. History is simulated with MemoryRouter + a navigate(-1) probe.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router";
import { describe, expect, it } from "vitest";
import {
  handleTabKeyDown,
  panelId,
  tabId,
  useFilterParams,
  useOffsetParam,
  useTabHash,
} from "./urlState.ts";

const TABS = ["overview", "console", "settings"] as const;

function TabProbe() {
  const [tab, setTab] = useTabHash(TABS);
  const loc = useLocation();
  const navigate = useNavigate();
  return (
    <div>
      <span data-testid="tab">{tab}</span>
      <span data-testid="hash">{loc.hash}</span>
      <span data-testid="search">{loc.search}</span>
      {TABS.map((name) => (
        <button key={name} type="button" onClick={() => setTab(name)}>
          {`go-${name}`}
        </button>
      ))}
      <button type="button" onClick={() => navigate(-1)}>
        back
      </button>
    </div>
  );
}

function OffsetProbe() {
  const [offset, setOffset] = useOffsetParam();
  const loc = useLocation();
  const navigate = useNavigate();
  return (
    <div>
      <span data-testid="offset">{offset}</span>
      <span data-testid="search">{loc.search}</span>
      <span data-testid="hash">{loc.hash}</span>
      <button type="button" onClick={() => setOffset(offset + 50)}>
        next
      </button>
      <button type="button" onClick={() => setOffset(Math.max(0, offset - 50))}>
        prev
      </button>
      <button type="button" onClick={() => navigate(-1)}>
        back
      </button>
    </div>
  );
}

const FILTER_KEYS = ["operation", "actor", "since", "until"] as const;

function FilterProbe() {
  const [filters, applyFilters] = useFilterParams(FILTER_KEYS);
  const loc = useLocation();
  const navigate = useNavigate();
  return (
    <div>
      <span data-testid="operation">{filters.operation}</span>
      <span data-testid="actor">{filters.actor}</span>
      <span data-testid="since">{filters.since}</span>
      <span data-testid="until">{filters.until}</span>
      <span data-testid="search">{loc.search}</span>
      <span data-testid="hash">{loc.hash}</span>
      <button
        type="button"
        onClick={() => applyFilters({ ...filters, operation: "member:add" })}
      >
        apply-op
      </button>
      <button
        type="button"
        onClick={() => applyFilters({ ...filters, actor: "alice" })}
      >
        apply-actor
      </button>
      <button
        type="button"
        onClick={() =>
          applyFilters({ operation: "", actor: "", since: "", until: "" })
        }
      >
        clear
      </button>
      <button type="button" onClick={() => navigate(-1)}>
        back
      </button>
    </div>
  );
}

describe("useTabHash", () => {
  it("defaults to the first tab with a clean (hash-less) URL", () => {
    render(
      <MemoryRouter initialEntries={["/x"]}>
        <TabProbe />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("tab").textContent).toBe("overview");
    expect(screen.getByTestId("hash").textContent).toBe("");
  });

  it("resolves the active tab from the URL hash (deep link)", () => {
    render(
      <MemoryRouter initialEntries={["/x#settings"]}>
        <TabProbe />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("tab").textContent).toBe("settings");
  });

  it("falls back to the default tab for an unknown hash", () => {
    render(
      <MemoryRouter initialEntries={["/x#bogus"]}>
        <TabProbe />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("tab").textContent).toBe("overview");
  });

  it("switching a tab writes its hash; the default tab clears it", () => {
    render(
      <MemoryRouter initialEntries={["/x"]}>
        <TabProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("go-console"));
    expect(screen.getByTestId("tab").textContent).toBe("console");
    expect(screen.getByTestId("hash").textContent).toBe("#console");

    fireEvent.click(screen.getByText("go-overview"));
    expect(screen.getByTestId("tab").textContent).toBe("overview");
    expect(screen.getByTestId("hash").textContent).toBe("");
  });

  it("Back restores the prior tab", () => {
    render(
      <MemoryRouter initialEntries={["/x"]}>
        <TabProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("go-console"));
    fireEvent.click(screen.getByText("go-settings"));
    expect(screen.getByTestId("tab").textContent).toBe("settings");

    fireEvent.click(screen.getByText("back"));
    expect(screen.getByTestId("tab").textContent).toBe("console");

    fireEvent.click(screen.getByText("back"));
    expect(screen.getByTestId("tab").textContent).toBe("overview");
  });

  it("re-clicking the active tab pushes no history entry", () => {
    render(
      <MemoryRouter initialEntries={["/x"]}>
        <TabProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("go-console"));
    expect(screen.getByTestId("tab").textContent).toBe("console");

    // Re-clicking the already-active tab must not grow history: a single Back
    // then lands back on the default, not on a duplicate console entry.
    fireEvent.click(screen.getByText("go-console"));
    fireEvent.click(screen.getByText("back"));
    expect(screen.getByTestId("tab").textContent).toBe("overview");
  });

  it("switching tabs drops a lingering offset; Back restores it", () => {
    render(
      <MemoryRouter initialEntries={["/x?offset=50#console"]}>
        <TabProbe />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("tab").textContent).toBe("console");

    // Each tab's pagination is independent state, so switching tabs drops the
    // offset param, leaving the new tab on a clean URL.
    fireEvent.click(screen.getByText("go-settings"));
    expect(screen.getByTestId("tab").textContent).toBe("settings");
    expect(screen.getByTestId("search").textContent).toBe("");

    // Back returns to the paginated tab with its offset intact (via history).
    fireEvent.click(screen.getByText("back"));
    expect(screen.getByTestId("tab").textContent).toBe("console");
    expect(screen.getByTestId("search").textContent).toBe("?offset=50");
  });
});

describe("useOffsetParam", () => {
  it("defaults to offset 0 with no query param", () => {
    render(
      <MemoryRouter initialEntries={["/x"]}>
        <OffsetProbe />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("offset").textContent).toBe("0");
    expect(screen.getByTestId("search").textContent).toBe("");
  });

  it("reads the offset from the query param (deep link)", () => {
    render(
      <MemoryRouter initialEntries={["/x?offset=50"]}>
        <OffsetProbe />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("offset").textContent).toBe("50");
  });

  it("paging round-trips the offset through the URL and Back restores it", () => {
    render(
      <MemoryRouter initialEntries={["/x"]}>
        <OffsetProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("next"));
    expect(screen.getByTestId("offset").textContent).toBe("50");
    expect(screen.getByTestId("search").textContent).toBe("?offset=50");

    fireEvent.click(screen.getByText("next"));
    expect(screen.getByTestId("offset").textContent).toBe("100");

    fireEvent.click(screen.getByText("back"));
    expect(screen.getByTestId("offset").textContent).toBe("50");
  });

  it("offset 0 clears the param (the first page stays clean)", () => {
    render(
      <MemoryRouter initialEntries={["/x?offset=50"]}>
        <OffsetProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("prev"));
    expect(screen.getByTestId("offset").textContent).toBe("0");
    expect(screen.getByTestId("search").textContent).toBe("");
  });

  it("setting the same offset pushes no history entry", () => {
    render(
      <MemoryRouter initialEntries={["/x#audit"]}>
        <OffsetProbe />
      </MemoryRouter>,
    );
    // At offset 0 the "prev" button calls setOffset(0): a no-op that must not
    // grow history, so a later Back from a real page lands on offset 0, not on
    // a duplicate entry.
    fireEvent.click(screen.getByText("prev"));
    fireEvent.click(screen.getByText("next"));
    fireEvent.click(screen.getByText("back"));
    expect(screen.getByTestId("offset").textContent).toBe("0");
  });

  it("preserves the hash when changing the offset", () => {
    render(
      <MemoryRouter initialEntries={["/x#audit"]}>
        <OffsetProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("next"));
    expect(screen.getByTestId("hash").textContent).toBe("#audit");
    expect(screen.getByTestId("search").textContent).toBe("?offset=50");
  });
});

describe("useFilterParams", () => {
  it("defaults to empty filters with a clean (param-less) URL", () => {
    render(
      <MemoryRouter initialEntries={["/x"]}>
        <FilterProbe />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("operation").textContent).toBe("");
    expect(screen.getByTestId("actor").textContent).toBe("");
    expect(screen.getByTestId("search").textContent).toBe("");
  });

  it("derives the applied filters from the query string (deep link / reload)", () => {
    render(
      <MemoryRouter initialEntries={["/x?operation=member%3Aadd&actor=alice"]}>
        <FilterProbe />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("operation").textContent).toBe("member:add");
    expect(screen.getByTestId("actor").textContent).toBe("alice");
  });

  it("applying writes the non-empty filters as params; the URL round-trips", () => {
    render(
      <MemoryRouter initialEntries={["/x"]}>
        <FilterProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("apply-op"));
    expect(screen.getByTestId("operation").textContent).toBe("member:add");
    const params = new URLSearchParams(
      screen.getByTestId("search").textContent ?? "",
    );
    expect(params.get("operation")).toBe("member:add");
    expect(params.get("actor")).toBeNull();
  });

  it("clearing all filters returns to a clean URL", () => {
    render(
      <MemoryRouter initialEntries={["/x?operation=member%3Aadd"]}>
        <FilterProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("clear"));
    expect(screen.getByTestId("operation").textContent).toBe("");
    expect(screen.getByTestId("search").textContent).toBe("");
  });

  it("applying filters resets the offset to 0 (drops the offset param)", () => {
    render(
      <MemoryRouter initialEntries={["/x?offset=100"]}>
        <FilterProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("apply-op"));
    const params = new URLSearchParams(
      screen.getByTestId("search").textContent ?? "",
    );
    expect(params.get("operation")).toBe("member:add");
    expect(params.get("offset")).toBeNull();
  });

  it("preserves the hash when applying filters", () => {
    render(
      <MemoryRouter initialEntries={["/x#audit"]}>
        <FilterProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("apply-op"));
    expect(screen.getByTestId("hash").textContent).toBe("#audit");
  });

  it("re-applying the same filter set pushes no history entry", () => {
    render(
      <MemoryRouter initialEntries={["/x"]}>
        <FilterProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("apply-op"));
    expect(screen.getByTestId("operation").textContent).toBe("member:add");

    // Re-applying the already-applied filter set must not grow history: a single
    // Back then lands on the empty initial state, not on a duplicate member:add
    // entry.
    fireEvent.click(screen.getByText("apply-op"));
    fireEvent.click(screen.getByText("back"));
    expect(screen.getByTestId("operation").textContent).toBe("");
  });

  it("Back restores the previous filter set", () => {
    render(
      <MemoryRouter initialEntries={["/x"]}>
        <FilterProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("apply-op"));
    fireEvent.click(screen.getByText("apply-actor"));
    expect(screen.getByTestId("operation").textContent).toBe("member:add");
    expect(screen.getByTestId("actor").textContent).toBe("alice");

    fireEvent.click(screen.getByText("back"));
    expect(screen.getByTestId("operation").textContent).toBe("member:add");
    expect(screen.getByTestId("actor").textContent).toBe("");

    fireEvent.click(screen.getByText("back"));
    expect(screen.getByTestId("operation").textContent).toBe("");
    expect(screen.getByTestId("actor").textContent).toBe("");
  });
});

// ── WAI-ARIA tab helpers (issue #1216) ────────────────────────────────────────

describe("tabId / panelId", () => {
  it("produces deterministic ids for tabs and panels", () => {
    expect(tabId("sd", "overview")).toBe("sd-tab-overview");
    expect(panelId("sd", "overview")).toBe("sd-panel-overview");
  });
});

// A probe that renders a real WAI-ARIA tab list with keyboard handling, so the
// handleTabKeyDown helper can be tested end-to-end in a DOM environment.
function AriaTabProbe() {
  const [tab, setTab] = useTabHash(TABS);
  return (
    <div>
      <div role="tablist">
        {TABS.map((name) => (
          <button
            key={name}
            id={tabId("t", name)}
            type="button"
            role="tab"
            aria-selected={tab === name}
            aria-controls={panelId("t", name)}
            tabIndex={tab === name ? 0 : -1}
            onClick={() => setTab(name)}
            onKeyDown={(e) => handleTabKeyDown(e, TABS, tab, setTab, "t")}
          >
            {name}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id={panelId("t", tab)}
        aria-labelledby={tabId("t", tab)}
      >
        {tab} content
      </div>
    </div>
  );
}

describe("handleTabKeyDown (WAI-ARIA keyboard navigation, #1216)", () => {
  function renderAriaProbe(hash = "") {
    render(
      <MemoryRouter initialEntries={[`/x${hash}`]}>
        <AriaTabProbe />
      </MemoryRouter>,
    );
  }

  it("ArrowRight moves focus and activates the next tab", () => {
    renderAriaProbe();
    const overview = screen.getByRole("tab", { name: "overview" });
    overview.focus();
    fireEvent.keyDown(overview, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "console" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "console" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("ArrowLeft moves focus and activates the previous tab", () => {
    renderAriaProbe("#console");
    const console = screen.getByRole("tab", { name: "console" });
    console.focus();
    fireEvent.keyDown(console, { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: "overview" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "overview" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("ArrowRight wraps from the last tab to the first", () => {
    renderAriaProbe("#settings");
    const settings = screen.getByRole("tab", { name: "settings" });
    settings.focus();
    fireEvent.keyDown(settings, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "overview" })).toHaveFocus();
  });

  it("ArrowLeft wraps from the first tab to the last", () => {
    renderAriaProbe();
    const overview = screen.getByRole("tab", { name: "overview" });
    overview.focus();
    fireEvent.keyDown(overview, { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: "settings" })).toHaveFocus();
  });

  it("Home focuses the first tab", () => {
    renderAriaProbe("#settings");
    const settings = screen.getByRole("tab", { name: "settings" });
    settings.focus();
    fireEvent.keyDown(settings, { key: "Home" });
    expect(screen.getByRole("tab", { name: "overview" })).toHaveFocus();
  });

  it("End focuses the last tab", () => {
    renderAriaProbe();
    const overview = screen.getByRole("tab", { name: "overview" });
    overview.focus();
    fireEvent.keyDown(overview, { key: "End" });
    expect(screen.getByRole("tab", { name: "settings" })).toHaveFocus();
  });

  it("tab buttons carry aria-controls linking to the panel", () => {
    renderAriaProbe();
    const overview = screen.getByRole("tab", { name: "overview" });
    expect(overview).toHaveAttribute("aria-controls", "t-panel-overview");
    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveAttribute("id", "t-panel-overview");
    expect(panel).toHaveAttribute("aria-labelledby", "t-tab-overview");
  });

  it("inactive tabs have tabIndex -1 (roving tabindex)", () => {
    renderAriaProbe();
    const overview = screen.getByRole("tab", { name: "overview" });
    const console = screen.getByRole("tab", { name: "console" });
    expect(overview).toHaveAttribute("tabindex", "0");
    expect(console).toHaveAttribute("tabindex", "-1");
  });
});
