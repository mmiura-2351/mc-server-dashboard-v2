package mc

import "io"

// byteSliceReader is a minimal io.Reader + io.ByteReader over an in-memory
// slice, used to parse a packet body that has already been read whole. It avoids
// pulling in bytes.Reader's full surface and keeps the parse offset local.
type byteSliceReader struct {
	buf []byte
	pos int
}

func newByteSliceReader(b []byte) *byteSliceReader {
	return &byteSliceReader{buf: b}
}

func (r *byteSliceReader) ReadByte() (byte, error) {
	if r.pos >= len(r.buf) {
		return 0, io.EOF
	}
	b := r.buf[r.pos]
	r.pos++
	return b, nil
}

func (r *byteSliceReader) Read(p []byte) (int, error) {
	if r.pos >= len(r.buf) {
		return 0, io.EOF
	}
	n := copy(p, r.buf[r.pos:])
	r.pos += n
	return n, nil
}
