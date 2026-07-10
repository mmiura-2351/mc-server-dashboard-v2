package game

import "sync"

// statusFlights coalesces concurrent status-cache misses per slug (issue
// #1720): the first miss leads the flight and performs the live status
// exchange; concurrent misses for the same slug wait for its result instead
// of each spawning their own ResolveJoin + Worker dial-back. Safe for
// concurrent use; the zero value is ready.
type statusFlights struct {
	mu      sync.Mutex
	flights map[string]*statusFlight
}

// statusFlight is one in-flight status exchange. json is written by the
// leader before done is closed; waiters read it only after <-done, so the
// channel close orders the accesses.
type statusFlight struct {
	done chan struct{}
	json string
}

// join returns the in-flight exchange for slug, creating one if none exists.
// leader is true for the creating caller, which must complete the exchange
// and call finish; other callers wait on the flight's done channel.
func (g *statusFlights) join(slug string) (f *statusFlight, leader bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if f, ok := g.flights[slug]; ok {
		return f, false
	}
	if g.flights == nil {
		g.flights = make(map[string]*statusFlight)
	}
	f = &statusFlight{done: make(chan struct{})}
	g.flights[slug] = f
	return f, true
}

// finish publishes the leader's result and releases all waiters. The entry is
// retired first so a miss arriving after the result was published starts a
// fresh flight rather than reading a stale one.
func (g *statusFlights) finish(slug string, f *statusFlight, statusJSON string) {
	g.mu.Lock()
	delete(g.flights, slug)
	g.mu.Unlock()
	f.json = statusJSON
	close(f.done)
}
