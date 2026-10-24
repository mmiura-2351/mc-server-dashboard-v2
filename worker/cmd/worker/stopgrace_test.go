package main

import (
	"bufio"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/adapters/containerdriver"
	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/application/instancemanager"
)

// composeWorkerStopGrace reads the worker service's stop_grace_period from the
// repository's compose.yaml. The file is read as text (the module carries no YAML
// dependency): the worker service is the block opened by a two-space "worker:" key
// and closed by the next two-space key.
func composeWorkerStopGrace(t *testing.T) time.Duration {
	t.Helper()
	f, err := os.Open(filepath.Join("..", "..", "..", "compose.yaml"))
	if err != nil {
		t.Fatalf("open compose.yaml: %v", err)
	}
	defer func() { _ = f.Close() }()
	inWorker := false
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		if strings.HasPrefix(line, "  ") && !strings.HasPrefix(line, "   ") && strings.HasSuffix(line, ":") {
			inWorker = line == "  worker:"
			continue
		}
		if !inWorker {
			continue
		}
		value, ok := strings.CutPrefix(strings.TrimSpace(line), "stop_grace_period:")
		if !ok {
			continue
		}
		d, err := time.ParseDuration(strings.TrimSpace(value))
		if err != nil {
			t.Fatalf("worker stop_grace_period %q: %v", value, err)
		}
		return d
	}
	if err := sc.Err(); err != nil {
		t.Fatalf("read compose.yaml: %v", err)
	}
	t.Fatal("compose.yaml has no stop_grace_period on the worker service")
	return 0
}

// The Worker's shutdown grace has to cover Manager.Close joining a retry stop at
// the driver's longest (issue #2934): a grace shorter than that lets Docker SIGKILL
// the Worker mid-stop. The progress-aware stop (issue #3436) lengthened that stop,
// so this pins the pair: change either and this test says which way it broke.
func TestComposeWorkerStopGraceCoversClose(t *testing.T) {
	grace := composeWorkerStopGrace(t)
	bound := instancemanager.CloseBound(containerdriver.MaxStopDuration)
	if grace < bound {
		t.Fatalf("compose.yaml worker stop_grace_period = %v, below Manager.Close's worst case %v "+
			"(containerdriver.MaxStopDuration %v + the save-on restore)", grace, bound, containerdriver.MaxStopDuration)
	}
}
