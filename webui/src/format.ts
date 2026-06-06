/** Small shared formatting helpers used across pages. */

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

// Compact heartbeat age, e.g. "2s ago" / "4m ago" / "3h ago". A negative or
// missing delta falls back to seconds so the cell always renders.
// Shared across the Overview and Workers fleet pages (#477).
export function heartbeatAge(iso: string): string {
  const seconds = Math.max(
    0,
    Math.round((Date.now() - Date.parse(iso)) / 1000),
  );
  if (seconds < 60) {
    return `${seconds}s ago`;
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  return `${Math.round(minutes / 60)}h ago`;
}
