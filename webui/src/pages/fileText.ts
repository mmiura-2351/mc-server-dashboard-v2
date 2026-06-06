/**
 * Base64 ⇄ UTF-8 text helpers and text-vs-binary detection for the Files tab.
 *
 * The file routes carry content base64-encoded (bytes-faithful, no encoding
 * mangling on the wire — servers/api/files.py). The browser's `btoa`/`atob`
 * operate on Latin-1 "binary strings", so a bare `btoa(unicode)` throws and a
 * bare `atob` mangles multi-byte UTF-8. We bridge through `TextEncoder`/
 * `TextDecoder` so the editor round-trips UTF-8 (e.g. an MOTD with emoji) byte
 * for byte.
 *
 * Text-vs-binary rule: sniff the decoded byte prefix for a NUL (0x00). Real
 * text files (server.properties, JSON, YAML, logs) never contain a NUL byte,
 * while compiled/compressed binaries (region files, JARs, images) reliably do
 * near the start. A NUL in the first {@link SNIFF_BYTES} bytes ⇒ binary
 * (download only); otherwise the file opens in the editor. This is a content
 * sniff rather than an extension allowlist so an unknown-extension text file
 * still edits and a `.txt`-named blob still does not.
 */

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

/** Decode a base64 payload as UTF-8 text. */
export function decodeBase64Utf8(base64: string): string {
  return new TextDecoder().decode(base64ToBytes(base64));
}

/** Encode UTF-8 text to a base64 payload. */
export function encodeUtf8Base64(text: string): string {
  return bytesToBase64(new TextEncoder().encode(text));
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
