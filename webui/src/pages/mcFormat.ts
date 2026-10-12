/** Strip extended hex sequences before standard section-sign codes so their inner pairs are not partially matched. */
export function stripMinecraftCodes(text: string): string {
  return text.replace(/§x(§[0-9a-f]){6}/gi, "").replace(/§[0-9a-fk-or]/gi, "");
}
