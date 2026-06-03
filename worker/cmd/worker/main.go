// Command worker is the entry point (the edge / wiring layer) of the Worker
// execution agent. It is a stub: dependency injection, the gRPC stream client,
// and execution drivers are added in later changes.
package main

import (
	"fmt"
	"io"
	"os"
)

// banner is what the stub entry point reports until the real wiring lands.
const banner = "mc-server-dashboard worker: not implemented"

func run(w io.Writer) error {
	_, err := fmt.Fprintln(w, banner)
	return err
}

func main() {
	if err := run(os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
