package bedrocktunnel

import (
	"encoding/binary"
	"fmt"
	"io"
)

// lengthPrefixSize is the size, in bytes, of the big-endian length prefix on
// each framed handshake message on the QUIC stream (docs/app/BEDROCK_TUNNEL.md
// Section 4: "Stream wire format").
const lengthPrefixSize = 4

// writeFramed writes data as one length-prefixed message: a 4-byte big-endian
// length followed by the bytes themselves.
func writeFramed(w io.Writer, data []byte) error {
	var lenBuf [lengthPrefixSize]byte
	binary.BigEndian.PutUint32(lenBuf[:], uint32(len(data)))
	if _, err := w.Write(lenBuf[:]); err != nil {
		return err
	}
	_, err := w.Write(data)
	return err
}

// readFramed reads one length-prefixed message. A declared length over
// maxBytes is rejected without reading further, so a misbehaving peer cannot
// make the reader buffer unbounded data.
func readFramed(r io.Reader, maxBytes int) ([]byte, error) {
	var lenBuf [lengthPrefixSize]byte
	if _, err := io.ReadFull(r, lenBuf[:]); err != nil {
		return nil, err
	}
	n := binary.BigEndian.Uint32(lenBuf[:])
	if n > uint32(maxBytes) {
		return nil, fmt.Errorf("bedrocktunnel: frame too large (%d bytes, max %d)", n, maxBytes)
	}
	buf := make([]byte, n)
	if _, err := io.ReadFull(r, buf); err != nil {
		return nil, err
	}
	return buf, nil
}
