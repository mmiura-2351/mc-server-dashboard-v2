/**
 * Base64 ⇄ text helpers and text-vs-binary detection for the Files tab.
 *
 * The file routes carry content base64-encoded (bytes-faithful, no encoding
 * mangling on the wire — servers/api/files.py). The browser's `btoa`/`atob`
 * operate on Latin-1 "binary strings", so a bare `btoa(unicode)` throws and a
 * bare `atob` mangles multi-byte UTF-8. We bridge through `TextEncoder`/
 * `TextDecoder` so the editor round-trips UTF-8 (e.g. an MOTD with emoji) byte
 * for byte.
 *
 * Charset rule (issue #2851): a file is read as UTF-8 when its bytes are valid
 * UTF-8, and as latin-1 (ISO-8859-1) otherwise, and written back in the charset
 * it was read in. That is how Minecraft 1.20+ reads `server.properties` — the
 * file whose non-UTF-8 bytes are ordinary, #2623 — and it keeps the round trip
 * lossless for every file: latin-1 maps each byte to one character and back,
 * where a UTF-8-only decode turned each invalid byte into U+FFFD for good.
 * The one exception mirrors the older reader: a pre-1.20 server reads its root
 * `server.properties` with `Properties.load(InputStream)`, latin-1 only, so
 * that file on that server is read and written as latin-1 whatever its bytes
 * are — written as UTF-8, a non-ASCII character reaches the server mangled.
 *
 * Text-vs-binary rule: sniff the decoded byte prefix for a NUL (0x00). Real
 * text files (server.properties, JSON, YAML, logs) never contain a NUL byte,
 * while compiled/compressed binaries (region files, JARs, images) reliably do
 * near the start. A NUL in the first {@link SNIFF_BYTES} bytes ⇒ binary
 * (download only); otherwise the file opens in the editor. This is a content
 * sniff rather than an extension allowlist so an unknown-extension text file
 * still edits and a `.txt`-named blob still does not.
 */

import { readsServerPropertiesAsUtf8 } from "../mcVersion.ts";

/** Bytes of the decoded prefix inspected for the NUL-byte binary signal. */
const SNIFF_BYTES = 8192;

/** Decode a base64 string to its raw bytes. */
function base64ToBytes(base64: string): Uint8Array {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

/** Encode raw bytes to a base64 string. */
function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

/** The charset a file's bytes were read in, and are written back in. */
export type TextCharset = "utf-8" | "latin-1";

/** A file's content as text, with the charset it was read in. */
export interface DecodedText {
  text: string;
  charset: TextCharset;
}

/** Text holds a character its file's charset cannot encode. */
export class UnencodableTextError extends Error {
  constructor() {
    super("text holds a character outside latin-1");
    this.name = "UnencodableTextError";
  }
}

/**
 * Decode a base64 payload as text in the charset the charset rule above picks
 * for `file`: its working-set path and its server's Minecraft version.
 */
export function decodeBase64Text(
  base64: string,
  file: { path: string; mcVersion: string | null | undefined },
): DecodedText {
  // Only the root server.properties is the file the server itself reads.
  const latin1Only =
    file.path === "server.properties" &&
    !readsServerPropertiesAsUtf8(file.mcVersion);
  if (!latin1Only) {
    try {
      const text = new TextDecoder("utf-8", { fatal: true }).decode(
        base64ToBytes(base64),
      );
      return { text, charset: "utf-8" };
    } catch {
      // Not UTF-8: read it as latin-1 below.
    }
  }
  // `atob`'s binary string IS latin-1: one character per byte, same value.
  // Not `new TextDecoder("latin1")`, which WHATWG maps to windows-1252.
  return { text: atob(base64), charset: "latin-1" };
}

/** Encode UTF-8 text to a base64 payload. */
export function encodeUtf8Base64(text: string): string {
  return bytesToBase64(new TextEncoder().encode(text));
}

/**
 * Encode text to a base64 payload in `charset`.
 *
 * @throws UnencodableTextError when `charset` is latin-1 and the text holds a
 *   character above U+00FF.
 */
export function encodeTextBase64(text: string, charset: TextCharset): string {
  if (charset === "utf-8") {
    return encodeUtf8Base64(text);
  }
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) > 0xff) {
      throw new UnencodableTextError();
    }
  }
  return btoa(text);
}

/**
 * Whether the base64 payload is probably an editable text file: true unless a
 * NUL byte appears in its first {@link SNIFF_BYTES} decoded bytes.
 */
export function isProbablyText(base64: string): boolean {
  const bytes = base64ToBytes(base64);
  const limit = Math.min(bytes.length, SNIFF_BYTES);
  for (let i = 0; i < limit; i++) {
    if (bytes[i] === 0) {
      return false;
    }
  }
  return true;
}
