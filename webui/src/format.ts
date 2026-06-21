/** Small shared formatting helpers used across pages. */

import type { TranslationKey } from "./i18n/index.ts";

/** Human-readable byte size (binary units), e.g. 1610612736 → "1.5 GiB". */
export function humanizeBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

// Render an ISO timestamp in the viewer's locale and timezone, e.g. a raw
// `2026-06-05T13:46:35.411582Z` → "6/5/2026, 1:46:35 PM". Mirrors the audit
// log's inline `toLocaleString()` (auditShared.tsx) so "created" times read
// consistently across the app (#644).
export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

// Shorten a UUID to its leading segment for display, keeping the full id for a
// hover title. The API exposes no friendly name for workers (WorkerResponse is
// id-only) nor for a backup's author (created_by is a bare UUID), so those ids
// are labelled and abbreviated rather than dumped in full (#644).
export function shortId(id: string): string {
  return id.split("-")[0];
}

// The pill class mirrors the workers mockup: online → running (green),
// draining → starting (amber), anything else (offline) → crashed (red).
// Shared across the Overview and Workers fleet pages (#477) so the mapping
// lives in one place.
export function statusPill(status: string): string {
  if (status === "online") {
    return "running";
  }
  if (status === "draining") {
    return "starting";
  }
  return "crashed";
}

// Convert a Maven-style version interval (e.g. "[1.9.10,)") to a
// human-readable string (e.g. "1.9.10+"). Non-Maven ranges (fabric semver,
// plain versions) pass through unchanged. Wildcard / empty returns "" (#1361).
const MAVEN_RE = /^([[(])\s*([^,]*?)\s*,\s*([^\])]*?)\s*([\])])$/;

export function formatRange(range: string): string {
  if (!range || range === "*") return "";

  const m = MAVEN_RE.exec(range);
  if (!m) return range;

  const [, , lo, hi, closeBracket] = m;

  // Lower-bounded only: [lo,) → "lo+"
  if (lo && !hi) {
    return `${lo}+`;
  }
  // Upper-bounded only: (,hi) → "< hi" or (,hi] → "<= hi"
  if (!lo && hi) {
    return closeBracket === "]" ? `<= ${hi}` : `< ${hi}`;
  }
  // Both bounds: range display
  if (lo && hi) {
    return `${lo} – ${hi}`;
  }
  // (,) — degenerate, pass through
  return range;
}

// Compact heartbeat age, e.g. "2s ago" / "4m ago" / "3h ago". A negative or
// missing delta falls back to seconds so the cell always renders.
// Shared across the Overview and Workers fleet pages (#477).
// Accepts a translation function so the output respects the active locale
// (#1214).
export function heartbeatAge(
  iso: string,
  tr: (key: TranslationKey) => string,
): string {
  const seconds = Math.max(
    0,
    Math.round((Date.now() - Date.parse(iso)) / 1000),
  );
  if (seconds < 60) {
    return tr("format.secondsAgo").replace("{value}", String(seconds));
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return tr("format.minutesAgo").replace("{value}", String(minutes));
  }
  return tr("format.hoursAgo").replace(
    "{value}",
    String(Math.round(minutes / 60)),
  );
}
