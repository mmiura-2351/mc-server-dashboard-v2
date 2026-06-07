/**
 * Unit tests for the URL-driven view-state hooks (#514): the active tab lives in
 * the hash, the page offset in `?offset=N`, both drive history so Back restores
 * the prior view. History is simulated with MemoryRouter + a navigate(-1) probe.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router";
import { describe, expect, it } from "vitest";
import { useOffsetParam, useTabHash } from "./urlState.ts";

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
