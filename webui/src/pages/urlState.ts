import { type KeyboardEvent, useCallback } from "react";
import { useLocation, useNavigate } from "react-router";

/** Use the first tab for missing/unknown hashes; selecting it clears the hash and other selections push history. */
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
      if (tab === active) return;
      // Drop pagination on tab switch; browser Back restores the prior tab's page from history.
      const hash = tab === tabs[0] ? "" : `#${tab}`;
      navigate(`${location.pathname}${hash}`);
    },
    [navigate, location.pathname, active, tabs],
  );

  return [active, setTab];
}

export function useOffsetParam(): [number, (offset: number) => void] {
  const location = useLocation();
  const navigate = useNavigate();
  const raw = Number(new URLSearchParams(location.search).get("offset"));
  const offset = Number.isInteger(raw) && raw > 0 ? raw : 0;

  const setOffset = useCallback(
    (next: number) => {
      const target = next > 0 ? next : 0;
      if (target === offset) return;
      const params = new URLSearchParams(location.search);
      if (next <= 0) {
        params.delete("offset");
      } else {
        params.set("offset", String(next));
      }
      const search = params.toString();
      navigate(
        `${location.pathname}${search ? `?${search}` : ""}${location.hash}`,
      );
    },
    [navigate, location.pathname, location.search, location.hash, offset],
  );

  return [offset, setOffset];
}

/** Store applied filters in the URL so links and reloads restore them.
 * Changing filters resets pagination, preserves the tab hash, and pushes history. */
export function useFilterParams<K extends string>(
  keys: readonly K[],
): [Record<K, string>, (next: Record<K, string>) => void] {
  const location = useLocation();
  const navigate = useNavigate();
  const params = new URLSearchParams(location.search);
  const filters = Object.fromEntries(
    keys.map((key) => [key, params.get(key) ?? ""]),
  ) as Record<K, string>;

  const setFilters = useCallback(
    (next: Record<K, string>) => {
      const params = new URLSearchParams(location.search);
      for (const key of keys) {
        const value = next[key].trim();
        if (value === "") {
          params.delete(key);
        } else {
          params.set(key, value);
        }
      }
      params.delete("offset");
      const search = params.toString();
      if (search === new URLSearchParams(location.search).toString()) return;
      navigate(
        `${location.pathname}${search ? `?${search}` : ""}${location.hash}`,
      );
    },
    [navigate, location.pathname, location.search, location.hash, keys],
  );

  return [filters, setFilters];
}

/** Store directory and file in URL history; omit root/closed values and preserve unrelated parameters. */
export function useFileBrowserParams(): [
  { dir: string; file: string | null },
  (
    nextDir: string,
    nextFile: string | null,
    options?: { replace?: boolean },
  ) => void,
] {
  const location = useLocation();
  const navigate = useNavigate();
  const params = new URLSearchParams(location.search);
  const dir = params.get("dir") ?? "";
  const file = params.get("file") ?? null;

  const setParams = useCallback(
    (
      nextDir: string,
      nextFile: string | null,
      options?: { replace?: boolean },
    ) => {
      const currentDir = new URLSearchParams(location.search).get("dir") ?? "";
      const currentFile =
        new URLSearchParams(location.search).get("file") ?? null;
      if (currentDir === nextDir && currentFile === nextFile) return;

      const next = new URLSearchParams(location.search);
      if (nextDir === "") {
        next.delete("dir");
      } else {
        next.set("dir", nextDir);
      }
      if (nextFile === null) {
        next.delete("file");
      } else {
        next.set("file", nextFile);
      }
      const search = next.toString();
      navigate(
        `${location.pathname}${search ? `?${search}` : ""}${location.hash}`,
        { replace: options?.replace },
      );
    },
    [navigate, location.pathname, location.search, location.hash],
  );

  return [{ dir, file }, setParams];
}

export function tabId(prefix: string, name: string): string {
  return `${prefix}-tab-${name}`;
}

export function panelId(prefix: string, name: string): string {
  return `${prefix}-panel-${name}`;
}

/** Arrow keys and Home/End move focus and activate tabs, keeping the URL hash in sync. */
export function handleTabKeyDown<T extends string>(
  event: KeyboardEvent<HTMLButtonElement>,
  tabs: readonly T[],
  active: T,
  setTab: (tab: T) => void,
  prefix: string,
): void {
  const idx = (tabs as readonly string[]).indexOf(active);
  let next: T | undefined;
  switch (event.key) {
    case "ArrowRight":
      next = tabs[(idx + 1) % tabs.length];
      break;
    case "ArrowLeft":
      next = tabs[(idx - 1 + tabs.length) % tabs.length];
      break;
    case "Home":
      next = tabs[0];
      break;
    case "End":
      next = tabs[tabs.length - 1];
      break;
    default:
      return;
  }
  event.preventDefault();
  setTab(next);
  document.getElementById(tabId(prefix, next))?.focus();
}
