/**
 * Size limits the API enforces, held here so the UI can say so before a round
 * trip (issue #3363). The generated `schema.ts` carries types only, so these
 * are constants; `limits.test.ts` pins each to the `maxLength` the OpenAPI
 * document declares.
 */

/**
 * The longest console line, in UTF-8 bytes: what one RCON command carries.
 * Bounds the console input and a command schedule's command.
 */
export const MAX_CONSOLE_LINE_BYTES = 1446;

/** The longest schedule warning message; it is broadcast as `say <message>`. */
export const MAX_WARNING_MESSAGE_BYTES = 1442;

/** The largest file the editor can save (larger files are uploaded instead). */
export const MAX_EDIT_BYTES = 4 * 1024 * 1024;

/** The size of `text` as the API measures it: its UTF-8 encoding, in bytes. */
export function utf8ByteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}
