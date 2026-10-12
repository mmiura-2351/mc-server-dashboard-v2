import { useCallback, useSyncExternalStore } from "react";
import type { UploadProgress } from "../api/client.ts";

/** Keep upload state at module scope so progress survives tab unmounts; useSyncExternalStore updates React. */
export interface UploadProgressState {
  active: boolean;
  loaded: number;
  total: number;
  percent: number;
  elapsedMs: number;
  /** Return the fresh signal synchronously; pass it to the upload so cancel aborts that request. */
  start: (total: number) => AbortSignal;
  onProgress: UploadProgress;
  reset: () => void;
  cancel: () => void;
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

// Shared upload state survives component unmounts.
let state: UploadState = { ...initialState };
let abortController = new AbortController();
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

function startUpload(total: number): AbortSignal {
  // A fresh controller keeps previous cancellation from aborting the next upload.
  abortController = new AbortController();
  const t = Date.now();
  state = { active: true, loaded: 0, total, startedAt: t, now: t };
  emitChange();
  return abortController.signal;
}

function onUploadProgress(loaded: number, total: number) {
  state = {
    ...state,
    loaded,
    // Prefer XHR's total once known; otherwise retain the initial file-size estimate.
    total: total > 0 ? total : state.total,
    now: Date.now(),
  };
  emitChange();
}

function resetUpload() {
  state = { ...initialState };
  emitChange();
}

function cancelUpload() {
  abortController.abort();
  resetUpload();
}

export function _resetForTesting() {
  state = { ...initialState };
  abortController = new AbortController();
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
    cancel: useCallback(cancelUpload, []),
  };
}
