/**
 * Minecraft version comparison helpers.
 *
 * `require-resource-pack` and `resource-pack-prompt` in server.properties
 * were added in Minecraft 1.17. This module exposes a predicate so the UI
 * can hide those fields on older servers, and one for the charset a server
 * reads its server.properties in, which changed in 1.20.
 */

/**
 * Returns `true` when `mcVersion` is >= 1.17, a snapshot, or null/undefined.
 *
 * Parsing rules:
 * - Standard release: `"1.16.4"` -> major=1, minor=16 -> false
 * - Snapshot (e.g. `"24w03a"`, `"21w15a"`) -> true (treat as latest)
 * - Null / undefined / empty -> true (safe default: show fields)
 */
export function supportsResourcePackOptions(
  mcVersion: string | null | undefined,
): boolean {
  if (mcVersion == null || mcVersion === "") return true;

  const match = mcVersion.match(/^(\d+)\.(\d+)(?:\.\d+)?$/);
  if (!match) {
    // Non-standard format (snapshot, pre-release, etc.) — treat as latest.
    return true;
  }

  const major = Number(match[1]);
  const minor = Number(match[2]);
  return major > 1 || (major === 1 && minor >= 17);
}

/**
 * Returns `true` when a server of `mcVersion` reads its server.properties as
 * UTF-8 first: 1.20 or later, a snapshot, or null/undefined (issue #2851).
 * Below 1.20 it reads that file as ISO-8859-1 only
 * (`Properties.load(InputStream)`).
 *
 * Parsing follows {@link supportsResourcePackOptions}: a non-standard format
 * (snapshot, pre-release) or a null / empty version is treated as latest.
 */
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
