/**
 * URL-driven view state for tabs and pagination (issue #514). In-page state
 * (the active tab, the current page) lives in the URL so the browser Back
 * button restores the previous view rather than jumping to the previous route.
 *
 * Conventions, following the WEBUI_SPEC.md Section 5 screen map (which names
 * hash sub-tabs such as `#overview` / `#members`) and the table endpoints'
 * limit/offset windows:
 * - tabs → URL hash fragment (`#files`); the default tab keeps a clean URL.
 * - page offset → `?offset=N` query param; offset 0 keeps a clean URL.
 *
 * Both reads derive purely from the URL, and writes go through `navigate`, so
 * there is no URL→state→URL echo loop (the AppShell useUrlCommunitySync pattern
 * is the prior art for keeping the URL authoritative).
 */

import { useCallback } from "react";
import { useLocation, useNavigate } from "react-router";

/**
 * Drive the active tab from the URL hash. `tabs` is the allowed set and its
 * first entry is the default. A missing/unknown hash resolves to the default;
 * selecting the default clears the hash so the canonical URL stays clean. Tab
 * switches push a history entry, so Back walks the tab history.
 */
export function useTabHash<T extends string>(
  tabs: readonly T[],
): [T, (tab: T) => void] {
  const location = useLocation();
  const navigate = useNavigate();
  const raw = location.hash.replace(/^#/, "");
  const active = (tabs as readonly string[]).includes(raw)
    ? (raw as T)
    : tabs[0];

  const setTab = useCallback(
    (tab: T) => {
      // Re-selecting the active tab is a no-op: skip the navigate so it does not
      // push a duplicate history entry (which would make Back a double press).
      if (tab === active) return;
      // The default tab is the clean (hash-less) URL; other tabs carry their
      // hash. Each tab's pagination is independent state, so drop any offset
      // param when switching tabs — a Back to the paginated tab restores it
      // through history.
      const hash = tab === tabs[0] ? "" : `#${tab}`;
      navigate(`${location.pathname}${hash}`);
    },
    [navigate, location.pathname, active, tabs],
  );

  return [active, setTab];
}

/**
 * Drive a page offset from the `?offset=N` query param. Offset 0 (the first
 * page) keeps the param out of the URL. Setting the offset pushes a history
 * entry so Back restores the prior page. Other query params are preserved.
 */
export function useOffsetParam(): [number, (offset: number) => void] {
  const location = useLocation();
  const navigate = useNavigate();
  const raw = Number(new URLSearchParams(location.search).get("offset"));
  const offset = Number.isInteger(raw) && raw > 0 ? raw : 0;

  const setOffset = useCallback(
    (next: number) => {
      // Setting the current offset is a no-op: skip the navigate so it does not
      // push a duplicate history entry (which would make Back a double press).
      const target = next > 0 ? next : 0;
      if (target === offset) return;
      const params = new URLSearchParams(location.search);
      if (next <= 0) {
        params.delete("offset");
      } else {
        params.set("offset", String(next));
      }
      const search = params.toString();
      // Preserve the current hash (a paginated table can live inside a
      // hash-driven tab) and push a history entry so Back restores the page.
      navigate(
        `${location.pathname}${search ? `?${search}` : ""}${location.hash}`,
      );
    },
    [navigate, location.pathname, location.search, location.hash, offset],
  );

  return [offset, setOffset];
}
