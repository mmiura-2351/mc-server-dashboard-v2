import { useCallback, useRef, useState } from "react";

export type NavState = { dir: string; openFile: string | null };

/** Keep navigation callbacks stable and discard forward history when navigating after Back. */
export function useNavHistory(
  /** Initial state is read only on mount; later changes are ignored. */
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

  const goBack = useCallback((): NavState => {
    const st = stateRef.current;
    if (st.index > 0) {
      st.index -= 1;
      forceRender((n) => n + 1);
      return st.history[st.index];
    }
    return st.history[st.index];
  }, []);

  const goForward = useCallback((): NavState => {
    const st = stateRef.current;
    if (st.index < st.history.length - 1) {
      st.index += 1;
      forceRender((n) => n + 1);
      return st.history[st.index];
    }
    return st.history[st.index];
  }, []);

  /** External URL navigation reuses a matching entry or replaces the current one without discarding forward history. */
  const jumpTo = useCallback((next: NavState) => {
    const st = stateRef.current;
    const cur = st.history[st.index];
    if (next.dir === cur.dir && next.openFile === cur.openFile) return;
    const matchIdx = st.history.findIndex(
      (e) => e.dir === next.dir && e.openFile === next.openFile,
    );
    if (matchIdx !== -1) {
      st.index = matchIdx;
    } else {
      const newEntries = [...st.history];
      newEntries[st.index] = next;
      st.history = newEntries;
    }
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
