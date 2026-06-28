import { useCallback, useSyncExternalStore } from "react";
import type { UploadProgress } from "../api/client.ts";

/**
 * Tracks bytes-uploaded / total and elapsed time for a single upload, shared by
 * the four upload sites (issue #1207). `start(total)` arms it with the file size
 * and records the start time; `onProgress` is the {@link UploadProgress}
 * callback handed to `postFormWithProgress`; `reset` returns it to idle. The
 * derived `percent` and `elapsedMs` drive the shared `UploadProgress` bar.
 *
 * State is held at module scope so it survives component unmount/remount cycles
 * (e.g. tab switches). React is kept in sync via `useSyncExternalStore`.
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

interface UploadState {
  active: boolean;
  loaded: number;
  total: number;
  startedAt: number;
  now: number;
}

const initialState: UploadState = {
  active: false,
  loaded: 0,
  total: 0,
  startedAt: 0,
  now: 0,
};

// Module-level singleton — survives component mount/unmount cycles.
let state: UploadState = { ...initialState };
const listeners = new Set<() => void>();

function emitChange() {
  for (const listener of listeners) {
    listener();
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot(): UploadState {
  return state;
}

function startUpload(total: number) {
  const t = Date.now();
  state = { active: true, loaded: 0, total, startedAt: t, now: t };
  emitChange();
}

function onUploadProgress(loaded: number, total: number) {
  state = {
    ...state,
    loaded,
    // The XHR `total` is authoritative once the request is in flight; keep the
    // file-size estimate from `start` when the event reports an unknown size.
    total: total > 0 ? total : state.total,
    now: Date.now(),
  };
  emitChange();
}

function resetUpload() {
  state = { ...initialState };
  emitChange();
}

/** Reset module-level state. Exported only for tests. */
export function _resetForTesting() {
  state = { ...initialState };
}

export function useUploadProgress(): UploadProgressState {
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const percent =
    snapshot.total > 0
      ? Math.min(100, Math.round((snapshot.loaded / snapshot.total) * 100))
      : 0;
  const elapsedMs = snapshot.active ? snapshot.now - snapshot.startedAt : 0;

  return {
    active: snapshot.active,
    loaded: snapshot.loaded,
    total: snapshot.total,
    percent,
    elapsedMs,
    start: useCallback(startUpload, []),
    onProgress: useCallback(onUploadProgress, []),
    reset: useCallback(resetUpload, []),
  };
}
