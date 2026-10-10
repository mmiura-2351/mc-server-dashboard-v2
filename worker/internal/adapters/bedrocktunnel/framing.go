package bedrocktunnel

import (
	"encoding/binary"
	"fmt"
	"io"
)

// Handshake messages have a four-byte big-endian length prefix.
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

// Reject oversized frames before reading or allocating their payload.
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
