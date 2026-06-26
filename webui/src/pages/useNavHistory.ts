import { useCallback, useRef, useState } from "react";

export type NavState = { dir: string; openFile: string | null };

/**
 * Browser-like navigation history for the file browser.
 *
 * Tracks directory navigation and file opens in a linear history stack.
 * Back/forward buttons move the index; navigating to a new location after
 * going back clears the forward stack (standard browser behavior).
 *
 * The returned `navigate`, `goBack`, and `goForward` are referentially stable
 * (safe as useEffect deps without triggering re-runs on every render).
 */
export function useNavHistory(
  /** Initial state (read only on mount; later changes are ignored). */
  initial?: NavState,
) {
  const [, forceRender] = useState(0);
  const stateRef = useRef({
    history: [initial ?? { dir: "", openFile: null }] as NavState[],
    index: 0,
  });

  const s = stateRef.current;
  const current = s.history[s.index];
  const canGoBack = s.index > 0;
  const canGoForward = s.index < s.history.length - 1;

  const navigate = useCallback((next: NavState) => {
    const st = stateRef.current;
    const cur = st.history[st.index];
    if (next.dir === cur.dir && next.openFile === cur.openFile) return;
    st.history = [...st.history.slice(0, st.index + 1), next];
    st.index = st.history.length - 1;
    forceRender((n) => n + 1);
  }, []);

  /** Move back; returns the resulting state (or the current one if already at the start). */
  const goBack = useCallback((): NavState => {
    const st = stateRef.current;
    if (st.index > 0) {
      st.index -= 1;
      forceRender((n) => n + 1);
      return st.history[st.index];
    }
    return st.history[st.index];
  }, []);

  /** Move forward; returns the resulting state (or the current one if already at the end). */
  const goForward = useCallback((): NavState => {
    const st = stateRef.current;
    if (st.index < st.history.length - 1) {
      st.index += 1;
      forceRender((n) => n + 1);
      return st.history[st.index];
    }
    return st.history[st.index];
  }, []);

  /**
   * Replace the current position and clear forward history. Used when an
   * external source (browser back/forward) sets the state — it must not grow
   * the internal stack the way `navigate` does.
   */
  const jumpTo = useCallback((next: NavState) => {
    const st = stateRef.current;
    const cur = st.history[st.index];
    if (next.dir === cur.dir && next.openFile === cur.openFile) return;
    st.history = [...st.history.slice(0, st.index), next];
    st.index = st.history.length - 1;
    forceRender((n) => n + 1);
  }, []);

  return {
    current,
    navigate,
    goBack,
    goForward,
    jumpTo,
    canGoBack,
    canGoForward,
  };
}
