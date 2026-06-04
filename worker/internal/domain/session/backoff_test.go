package session

import (
	"testing"
	"time"
)

func TestBackoffGrowsExponentiallyAndCaps(t *testing.T) {
	b := Backoff{Initial: time.Second, Max: 30 * time.Second, Multiplier: 2.0}

	cases := []struct {
		attempt int
		want    time.Duration
	}{
		{0, 1 * time.Second},
		{1, 2 * time.Second},
		{2, 4 * time.Second},
		{3, 8 * time.Second},
		{4, 16 * time.Second},
		{5, 30 * time.Second}, // 32s capped to 30s
		{6, 30 * time.Second}, // stays capped
	}
	for _, tc := range cases {
		// randFloat = 1.0 would map to the full base; we test base directly via
		// the public Delay with a value approaching 1 by using base through the
		// boundary check below.
		if got := b.base(tc.attempt); got != tc.want {
			t.Errorf("base(%d) = %v, want %v", tc.attempt, got, tc.want)
		}
	}
}

func TestBackoffDelayStaysWithinJitterBounds(t *testing.T) {
	b := Backoff{Initial: time.Second, Max: 30 * time.Second, Multiplier: 2.0}

	for attempt := 0; attempt < 8; attempt++ {
		capBase := b.base(attempt)
		for _, r := range []float64{0.0, 0.25, 0.5, 0.999} {
			d := b.Delay(attempt, r)
			if d < 0 || d > capBase {
				t.Errorf("Delay(%d, %v) = %v, want within [0, %v]", attempt, r, d, capBase)
			}
		}
	}
}

func TestBackoffFullJitterZeroAndMax(t *testing.T) {
	b := Backoff{Initial: time.Second, Max: 30 * time.Second, Multiplier: 2.0}

	if d := b.Delay(2, 0.0); d != 0 {
		t.Errorf("Delay with randFloat 0 = %v, want 0", d)
	}
	// randFloat just below 1 approaches the full base for that attempt.
	if d := b.Delay(2, 0.999999); d > b.base(2) {
		t.Errorf("Delay near 1 = %v, exceeded base %v", d, b.base(2))
	}
}
