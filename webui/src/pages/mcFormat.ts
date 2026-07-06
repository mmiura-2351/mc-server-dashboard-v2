/**
 * Strip Minecraft §-formatting codes from text.
 *
 * Minecraft uses the section sign (§, U+00A7) as a formatting prefix.
 * Standard codes: §[0-9a-fk-or] (colors, bold, italic, etc.).
 * Extended hex colors: §x followed by six §[0-9a-f] pairs.
 *
 * Extended hex is stripped first so the inner §-pairs are not partially matched
 * by the standard-code pass.
 */
export function stripMinecraftCodes(text: string): string {
  return text.replace(/§x(§[0-9a-f]){6}/gi, "").replace(/§[0-9a-fk-or]/gi, "");
}
