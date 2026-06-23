import { useCallback, useState } from "react";
import type { UploadProgress } from "../api/client.ts";

/**
 * Tracks bytes-uploaded / total and elapsed time for a single upload, shared by
 * the four upload sites (issue #1207). `start(total)` arms it with the file size
 * and records the start time; `onProgress` is the {@link UploadProgress}
 * callback handed to `postFormWithProgress`; `reset` returns it to idle. The
 * derived `percent` and `elapsedMs` drive the shared `UploadProgress` bar.
 */
export interface UploadProgressState {
  active: boolean;
  loaded: number;
  total: number;
  percent: number;
  elapsedMs: number;
  start: (total: number) => void;
  onProgress: UploadProgress;
  reset: () => void;
}

export function useUploadProgress(): UploadProgressState {
  const [active, setActive] = useState(false);
  const [loaded, setLoaded] = useState(0);
  const [total, setTotal] = useState(0);
  const [startedAt, setStartedAt] = useState(0);
  const [now, setNow] = useState(0);

  const start = useCallback((bytes: number) => {
    const t = Date.now();
    setActive(true);
    setLoaded(0);
    setTotal(bytes);
    setStartedAt(t);
    setNow(t);
  }, []);

  const onProgress = useCallback<UploadProgress>((l, t) => {
    setLoaded(l);
    // The XHR `total` is authoritative once the request is in flight; keep the
    // file-size estimate from `start` when the event reports an unknown size.
    if (t > 0) {
      setTotal(t);
    }
    setNow(Date.now());
  }, []);

  const reset = useCallback(() => {
    setActive(false);
    setLoaded(0);
    setTotal(0);
    setStartedAt(0);
    setNow(0);
  }, []);

  const percent =
    total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0;
  const elapsedMs = active ? now - startedAt : 0;

  return {
    active,
    loaded,
    total,
    percent,
    elapsedMs,
    start,
    onProgress,
    reset,
  };
}
