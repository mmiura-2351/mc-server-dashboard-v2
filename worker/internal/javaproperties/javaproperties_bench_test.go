package javaproperties

// Benchmark: backslash-continuation accumulation (issue #3346).
//
// A tenant-written server.properties is parsed on every launch, RCON open,
// tunnel dial and player-count poll, bounded only by the reader's 4 MiB cap
// (serverproperties.MaxSize). Each input below is one logical line made of
// many short continued lines, at sizes up to that cap: ns/op growing in
// proportion to the size is the linear behaviour the parse must keep.

import (
	"fmt"
	"strings"
	"testing"
)

func BenchmarkParseContinuations(b *testing.B) {
	for _, input := range []struct {
		name  string
		first string
		line  string
	}{
		// Every line adds one byte to the logical line.
		{"short-lines", "", "k\\\n"},
		// Every line extends the backslash run the logical line ends in.
		{"backslash-runs", "k", "\\\\\\\n"},
	} {
		for _, size := range []int{64 << 10, 256 << 10, 1 << 20, 4 << 20} {
			data := []byte(input.first + strings.Repeat(input.line, size/len(input.line)))
			b.Run(fmt.Sprintf("%s/%dKiB", input.name, size>>10), func(b *testing.B) {
				b.SetBytes(int64(len(data)))
				for b.Loop() {
					Parse(data)
				}
			})
		}
	}
}
