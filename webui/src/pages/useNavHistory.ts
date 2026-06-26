import { useState } from "react";

export type NavState = { dir: string; openFile: string | null };

/**
 * Browser-like navigation history for the file browser.
 *
 * Tracks directory navigation and file opens in a linear history stack.
 * Back/forward buttons move the index; navigating to a new location after
 * going back clears the forward stack (standard browser behavior).
 */
export function useNavHistory() {
  const [history, setHistory] = useState<NavState[]>([
    { dir: "", openFile: null },
  ]);
  const [index, setIndex] = useState(0);

  const current = history[index];
  const canGoBack = index > 0;
  const canGoForward = index < history.length - 1;

  const navigate = (next: NavState) => {
    const newHistory = [...history.slice(0, index + 1), next];
    setHistory(newHistory);
    setIndex(newHistory.length - 1);
  };

  const goBack = () => {
    if (canGoBack) setIndex(index - 1);
  };

  const goForward = () => {
    if (canGoForward) setIndex(index + 1);
  };

  return { current, navigate, goBack, goForward, canGoBack, canGoForward };
}
