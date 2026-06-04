package hostprocess

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
)

// RealSpawn launches an OS process with exec.Cmd. It is the production spawnFunc;
// tests substitute a fake. The process inherits no stdin; its stdout/stderr are
// wired to os.Pipe read ends the driver captures into the per-instance log pump
// (FR-MON-2). Explicit pipes (not cmd.StdoutPipe) are used so the read side
// stays open until the child closes its fd at exit, independent of Wait — the
// supervisor calls Wait before draining the pipes, which cmd.StdoutPipe forbids.
func RealSpawn(_ context.Context, name string, args []string, dir string) (process, error) {
	// Not bound to ctx: the process must outlive the Start call. Lifetime is
	// managed via Stop (signals) and supervision.
	cmd := exec.Command(name, args...) //nolint:gosec // name/args come from configured runtimes and the server spec, not user input.
	cmd.Dir = dir

	outR, outW, err := os.Pipe()
	if err != nil {
		return nil, fmt.Errorf("hostprocess: stdout pipe: %w", err)
	}
	errR, errW, err := os.Pipe()
	if err != nil {
		_ = outR.Close()
		_ = outW.Close()
		return nil, fmt.Errorf("hostprocess: stderr pipe: %w", err)
	}
	cmd.Stdout = outW
	cmd.Stderr = errW

	if err := cmd.Start(); err != nil {
		_ = outR.Close()
		_ = outW.Close()
		_ = errR.Close()
		_ = errW.Close()
		return nil, fmt.Errorf("hostprocess: start %s: %w", name, err)
	}
	// The child holds its own dup of the write ends; close ours so the read ends
	// see EOF when the child exits (and not before).
	_ = outW.Close()
	_ = errW.Close()

	return &execProcess{cmd: cmd, stdout: outR, stderr: errR}, nil
}

// execProcess wraps an *exec.Cmd as a supervised process.
type execProcess struct {
	cmd    *exec.Cmd
	stdout *os.File
	stderr *os.File
}

func (p *execProcess) Wait() error {
	err := p.cmd.Wait()
	// Close the read ends so any scan goroutine still blocked on Read unblocks
	// even if the child leaked the write fd to a survivor; normally the child's
	// exit already delivered EOF.
	_ = p.stdout.Close()
	_ = p.stderr.Close()
	return err
}

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

func (p *execProcess) Stdout() io.Reader { return p.stdout }

func (p *execProcess) Stderr() io.Reader { return p.stderr }

func (p *execProcess) Pid() int {
	if p.cmd.Process == nil {
		return 0
	}
	return p.cmd.Process.Pid
}
