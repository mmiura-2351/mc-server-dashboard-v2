package containerdriver

import (
	"bytes"
	"fmt"
	"io"
	"log/slog"
	"os"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// This file keeps logs/forge-install.log bounded (issue #3282). One install
// prints 1–2 MB, a failing one is retried, and a crashed server is started again
// and again; appended to forever, the log outgrew the files API's read cap — the
// one way an operator reads it — and every snapshot and backup of the working
// set. So the log holds one start's install: it is emptied as the install
// starts and keeps every attempt of it, and once that would pass
// execution.ForgeInstallLogMaxBytes it keeps the start of the install, a line
// saying how much was omitted, and the latest output — the latest attempt whole
// when it fits, and always the end of the output, where the installer prints
// what it died of.

const (
	// installLogHeadBytes bounds the start of the install the log keeps above
	// the omission line: the installer's banner, versions and first steps.
	installLogHeadBytes = 64 * 1024
	// installLogOmissionReserve is the room the omission line is given.
	installLogOmissionReserve = 256
	// installLogTailBytes is the room for the latest output below it: more than
	// one install attempt prints.
	installLogTailBytes = execution.ForgeInstallLogMaxBytes - installLogHeadBytes - installLogOmissionReserve
)

// installLogOmission is the line between the start of the install and its latest
// output once the log has dropped what lay between them.
const installLogOmission = "[mcsd] omitted %d bytes of install output here to keep this log under %d MiB: " +
	"the start of the install is above, its latest output below\n"

// installLogFile is what an install log is written through: the opened file, or
// a test's stand-in for one that fails.
type installLogFile interface {
	io.WriterAt
	Truncate(size int64) error
}

// installLog is logs/forge-install.log for one start's install: every attempt
// of it, within execution.ForgeInstallLogMaxBytes. Its attempts write to it one
// after another, never at once (installLogFollow.finish).
type installLog struct {
	// f is the opened log, closed once the install is over; nil when it could
	// not be opened. file is what is written through, nil once a write failed.
	f      *os.File
	file   installLogFile
	logger *slog.Logger

	// size is the length of the file; total and attempt count the output of the
	// install and of its current attempt.
	size, total, attempt int64
	// headLen is the length of the whole lines within the first
	// installLogHeadBytes of the output: what is kept above the omission line.
	headLen int64
	// recent is the latest output, at least installLogTailBytes of it once the
	// install has printed that much.
	recent []byte
}

// startInstallLog opens the install log for a start's install and empties it.
// A log that cannot be opened inside the working set (openInstallLog) or emptied
// is logged at WARN, and the install goes on without it.
func (i *instance) startInstallLog() *installLog {
	l := &installLog{logger: i.logger.With("server_id", i.spec.ServerID, "path", execution.ForgeInstallLogRelpath)}
	f, err := openInstallLog(i.spec.WorkingDir)
	if err == nil {
		if err = f.Truncate(0); err != nil {
			_ = f.Close()
		}
	}
	if err != nil {
		l.logger.Warn("forge install log not written; continuing the install without it", "err", err)
		return l
	}
	l.f, l.file = f, f
	if i.installLogSink != nil {
		l.file = i.installLogSink(f)
	}
	return l
}

// beginAttempt marks the start of the next install attempt's output.
func (l *installLog) beginAttempt() {
	l.attempt = 0
}

// close closes the log once the install is over.
func (l *installLog) close() {
	if l.f != nil {
		_ = l.f.Close()
	}
}

// write adds p, the next of the install's output, to the log. Like the scan it
// is teed with, it never fails: the first failed write (a full or failing
// filesystem) is logged at WARN and drops the log for the rest of the install
// (issue #3744).
func (l *installLog) write(p []byte) {
	if l.file == nil {
		return
	}
	if l.total < installLogHeadBytes {
		if nl := bytes.LastIndexByte(p[:min(int64(len(p)), installLogHeadBytes-l.total)], '\n'); nl >= 0 {
			l.headLen = l.total + int64(nl) + 1
		}
	}
	l.total += int64(len(p))
	l.attempt += int64(len(p))
	l.recent = append(l.recent, p...)
	if len(l.recent) > 2*installLogTailBytes {
		l.recent = append(l.recent[:0], l.recent[len(l.recent)-installLogTailBytes:]...)
	}

	var err error
	if l.size+int64(len(p)) <= execution.ForgeInstallLogMaxBytes {
		_, err = l.file.WriteAt(p, l.size)
		l.size += int64(len(p))
	} else {
		err = l.compact()
	}
	if err != nil {
		l.logger.Warn("forge install log write failed; continuing the install without it", "err", err)
		l.file = nil
	}
}

// compact rewrites the log as its head, the omission line and the latest output,
// the output just written included. The latest output is the current attempt
// whole when it fits, and at least half the room otherwise, so the next
// compaction is that far off: the log is rewritten a few times an install, not
// once a write.
//
// The rewrite is written over the file before the file is cut to its end, never
// the other way round, and nothing is synced: a Worker that dies in between
// leaves a log under the bound that still holds the latest output, followed by
// some earlier output.
func (l *installLog) compact() error {
	keep := int64(installLogTailBytes / 2)
	if l.attempt > keep && l.attempt <= installLogTailBytes {
		keep = l.attempt
	}
	kept := l.recent[int64(len(l.recent))-keep:]
	if keep != l.attempt {
		// The cut falls inside the output: the kept part starts at its next line.
		if nl := bytes.IndexByte(kept, '\n'); nl >= 0 {
			kept = kept[nl+1:]
		}
	}
	omitted := l.total - l.headLen - int64(len(kept))
	body := append(fmt.Appendf(nil, installLogOmission, omitted, execution.ForgeInstallLogMaxBytes>>20), kept...)
	if _, err := l.file.WriteAt(body, l.headLen); err != nil {
		return err
	}
	l.size = l.headLen + int64(len(body))
	return l.file.Truncate(l.size)
}
