package hostprocess

import (
	"context"
	"fmt"
	"os"
	"os/exec"
)

// RealSpawn launches an OS process with exec.Cmd. It is the production spawnFunc;
// tests substitute a fake. The process inherits no stdin and writes stdout/stderr
// to the Worker's; log capture is FR-MON-2 (out of scope for this milestone).
func RealSpawn(_ context.Context, name string, args []string, dir string) (process, error) {
	// Not bound to ctx: the process must outlive the Start call. Lifetime is
	// managed via Stop (signals) and supervision.
	cmd := exec.Command(name, args...) //nolint:gosec // name/args come from configured runtimes and the server spec, not user input.
	cmd.Dir = dir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("hostprocess: start %s: %w", name, err)
	}
	return &execProcess{cmd: cmd}, nil
}

// execProcess wraps an *exec.Cmd as a supervised process.
type execProcess struct {
	cmd *exec.Cmd
}

func (p *execProcess) Wait() error { return p.cmd.Wait() }

func (p *execProcess) Signal(sig os.Signal) error {
	if p.cmd.Process == nil {
		return nil
	}
	return p.cmd.Process.Signal(sig)
}

func (p *execProcess) Kill() error {
	if p.cmd.Process == nil {
		return nil
	}
	return p.cmd.Process.Kill()
}
