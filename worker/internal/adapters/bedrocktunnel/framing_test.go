package bedrocktunnel

import (
	"bytes"
	"testing"
)

func TestFramedRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	if err := writeFramed(&buf, []byte("hello")); err != nil {
		t.Fatalf("writeFramed: %v", err)
	}
	got, err := readFramed(&buf, 64)
	if err != nil {
		t.Fatalf("readFramed: %v", err)
	}
	if string(got) != "hello" {
		t.Fatalf("readFramed = %q, want hello", got)
	}
}

func TestReadFramedRejectsOversizedFrame(t *testing.T) {
	var buf bytes.Buffer
	if err := writeFramed(&buf, make([]byte, 100)); err != nil {
		t.Fatalf("writeFramed: %v", err)
	}
	if _, err := readFramed(&buf, 10); err == nil {
		t.Fatal("readFramed accepted a frame over the max, want an error")
	}
}

func TestReadFramedRejectsTruncatedFrame(t *testing.T) {
	var buf bytes.Buffer
	buf.Write([]byte{0, 0, 0, 5}) // declares 5 bytes
	buf.Write([]byte("ab"))       // only 2 bytes follow
	if _, err := readFramed(&buf, 64); err == nil {
		t.Fatal("readFramed accepted a truncated frame, want an error")
	}
}
