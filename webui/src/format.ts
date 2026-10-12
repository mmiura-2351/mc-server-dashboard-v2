import type { TranslationKey } from "./i18n/index.ts";

/** Use binary byte units, e.g. 1610612736 becomes 1.5 GiB. */
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

export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

export function shortId(id: string): string {
  return id.split("-")[0];
}

export function statusPill(status: string): string {
  if (status === "online") {
    return "running";
  }
  if (status === "draining") {
    return "starting";
  }
  return "crashed";
}

// Display Maven intervals compactly; pass other version formats through and hide empty/wildcard ranges.
const MAVEN_RE = /^([[(])\s*([^,]*?)\s*,\s*([^\])]*?)\s*([\])])$/;

export function formatRange(range: string): string {
  if (!range || range === "*") return "";

  const m = MAVEN_RE.exec(range);
  if (!m) return range;

  const [, , lo, hi, closeBracket] = m;

  if (lo && !hi) {
    return `${lo}+`;
  }
  if (!lo && hi) {
    return closeBracket === "]" ? `<= ${hi}` : `< ${hi}`;
  }
  if (lo && hi) {
    return `${lo} – ${hi}`;
  }
  return range;
}

// Use the supplied translator for heartbeat ages; clamp future timestamps to zero.
export function heartbeatAge(
  iso: string,
  tr: (key: TranslationKey, params?: Record<string, string | number>) => string,
): string {
  const seconds = Math.max(
    0,
    Math.round((Date.now() - Date.parse(iso)) / 1000),
  );
  if (seconds < 60) {
    return tr("format.secondsAgo", { value: seconds });
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return tr("format.minutesAgo", { value: minutes });
  }
  return tr("format.hoursAgo", { value: Math.round(minutes / 60) });
}
