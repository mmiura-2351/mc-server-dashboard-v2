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

/**
 * The most body the API reads of a request without a valid credential, an
 * expired access token included (SECURITY.md Section 5): past it the request is
 * refused 413 `credential_required_for_body`. The client refreshes an expiring
 * token before it sends a larger body. It mirrors the API's
 * `ANONYMOUS_MAX_REQUEST_BODY_BYTES` (`http_body_limit.py`), which is not in
 * the OpenAPI document; `api/tests/test_http_body_credential.py` pins the two
 * together.
 */
export const ANONYMOUS_MAX_REQUEST_BODY_BYTES = 64 * 1024;

/** The size of `text` as the API measures it: its UTF-8 encoding, in bytes. */
export function utf8ByteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}
