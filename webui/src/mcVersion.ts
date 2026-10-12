/** Resource-pack options require 1.17; server.properties switches to UTF-8 handling in 1.20. */

/** Treat snapshots, unknown versions, and empty versions as latest when deciding whether to show options. */
export function supportsResourcePackOptions(
  mcVersion: string | null | undefined,
): boolean {
  if (mcVersion == null || mcVersion === "") return true;

  const match = mcVersion.match(/^(\d+)\.(\d+)(?:\.\d+)?$/);
  if (!match) {
    // Treat unrecognized version formats as latest.
    return true;
  }

  const major = Number(match[1]);
  const minor = Number(match[2]);
  return major > 1 || (major === 1 && minor >= 17);
}

/** Before 1.20, server.properties uses Latin-1 only; newer or unknown versions use UTF-8 first. */
export function readsServerPropertiesAsUtf8(
  mcVersion: string | null | undefined,
): boolean {
  if (mcVersion == null || mcVersion === "") return true;

  const match = mcVersion.match(/^(\d+)\.(\d+)(?:\.\d+)?$/);
  if (!match) return true;

  const major = Number(match[1]);
  const minor = Number(match[2]);
  return major > 1 || (major === 1 && minor >= 20);
}
