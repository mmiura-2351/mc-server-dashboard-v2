package instancemanager

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"

	"golang.org/x/sys/unix"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/nofollow"
)

// The generation marker lies in a working set the server's own code can write (issue
// #3336), so its value is authenticated (issue #3349): the Worker writes it as
//
//	v2 <generation> <hex HMAC-SHA256(key, server id || 8-byte big-endian generation)>
//
// under a key of its own, and believes a marker only when that MAC verifies for the
// server whose working set holds it. Anything else — a bare number, which is what
// every marker before this format was, a marker copied from another server's working
// set, or one whose generation was edited — reads as generation 0, which makes the API
// hydrate: the safe direction. Replaying a genuine older marker of the same server only
// yields a generation the Worker really stamped, which is never above the store's.
const generationMarkerVersion = "v2"

// generationKeyFile names the key in the scratch root. A container is given its own
// working set only, never the scratch root, so no server's code can read the key; and
// the key shares the scratch volume's lifetime, as the markers it signs do. The name
// starts with a dot, as every Worker-reserved entry beside the server-id directories
// does, and matches no prefix a boot sweep reclaims.
const generationKeyFile = ".generation-key"

// generationKeyTemp is the name a new key is written under before it is linked to
// generationKeyFile, so the key's name never holds a partial key.
const generationKeyTemp = ".generation-key.tmp"

// generationKeySize is the key's length: HMAC-SHA256's block size would allow more,
// but 32 random bytes are the digest's own strength.
const generationKeySize = 32

// maxWarnedUnverified bounds how many servers a GenerationKey remembers having warned
// about, so that a long-lived Worker cannot grow the set without limit. The oldest
// entry is forgotten first; a forgotten server is warned about again.
const maxWarnedUnverified = 256

// GenerationKey is the key the Worker signs and verifies its generation markers with.
// A key that could not be loaded is UNUSABLE: no marker verifies under it, so every
// held set is advertised at generation 0 and hydrates, and a marker written under it is
// still written — the launch guard needs one — but in a form that never verifies. One GenerationKey serves the whole
// process, so the warning about a marker that fails verification is logged once per
// server.
type GenerationKey struct {
	key []byte // nil when unusable

	mu     sync.Mutex
	warned map[string]struct{}
	order  []string // warned, oldest first
}

// LoadGenerationKey returns the key under scratchDir, creating it when absent. It
// never fails: a key that exists but cannot be used — unreadable, not a regular file,
// not exactly generationKeySize bytes — is logged at ERROR and left in place, and the
// key returned is unusable. It is not replaced, because a fresh key would silently
// invalidate every marker and leave the operator nothing to inspect; removing the file
// and restarting the Worker is the remedy, and it costs every held server one hydrate
// (STORAGE.md Section 8). A missing scratch root is created, as on a first boot.
func LoadGenerationKey(scratchDir string, log *slog.Logger) *GenerationKey {
	key, err := loadOrCreateGenerationKey(scratchDir)
	if err != nil {
		if log != nil {
			log.Error("generation key unusable: no generation marker is believed, so every held "+
				"server is advertised at generation 0 and hydrates; the key is left as it is (remove "+
				"it and restart the Worker to make a new one, see STORAGE.md Section 8)",
				"path", filepath.Join(scratchDir, generationKeyFile), "error", err)
		}
		return &GenerationKey{}
	}
	return &GenerationKey{key: key}
}

// loadOrCreateGenerationKey reads the key in scratchDir, or creates it when there is
// none. Both are done relative to the scratch root's descriptor, and the key's name is
// never followed as a link. The root itself is the operator's configured path, so a
// link to it is followed, as the edit staging's open of it does.
func loadOrCreateGenerationKey(scratchDir string) ([]byte, error) {
	if err := os.MkdirAll(scratchDir, 0o750); err != nil {
		return nil, err
	}
	dirFd, err := nofollow.OpenAt(unix.AT_FDCWD, scratchDir, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: scratchDir, Err: err}
	}
	defer func() { _ = unix.Close(dirFd) }()

	key, err := readGenerationKey(dirFd)
	if !errors.Is(err, os.ErrNotExist) {
		return key, err
	}
	return createGenerationKey(dirFd)
}

// readGenerationKey reads the key behind dirFd. An absent key satisfies
// errors.Is(err, os.ErrNotExist); anything else that is not generationKeySize bytes in
// a regular file is an error.
func readGenerationKey(dirFd int) ([]byte, error) {
	data, err := nofollow.ReadFileAt(dirFd, generationKeyFile, generationKeySize)
	if err != nil {
		return nil, err
	}
	if len(data) != generationKeySize {
		return nil, fmt.Errorf("%s holds %d bytes, want %d", generationKeyFile, len(data), generationKeySize)
	}
	return data, nil
}

// createGenerationKey creates a random key behind dirFd. It is written in full and
// fsynced under generationKeyTemp, then linked to generationKeyFile: the link fails if
// that name exists, so the create is exclusive, and a crash at any point leaves either
// no key or the whole key, never a partial one, which would read as unusable at every
// later boot. A temp a crash left is removed first; only the Worker writes the scratch
// root.
func createGenerationKey(dirFd int) ([]byte, error) {
	key := make([]byte, generationKeySize)
	if _, err := rand.Read(key); err != nil {
		return nil, err
	}
	_ = unix.Unlinkat(dirFd, generationKeyTemp, 0)
	fd, err := nofollow.OpenAt(dirFd, generationKeyTemp,
		unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0o600)
	if err != nil {
		return nil, &os.PathError{Op: "create", Path: generationKeyTemp, Err: err}
	}
	defer func() { _ = unix.Unlinkat(dirFd, generationKeyTemp, 0) }()
	f := os.NewFile(uintptr(fd), generationKeyTemp)
	_, werr := f.Write(key)
	serr := f.Sync()
	if err := errors.Join(werr, serr, f.Close()); err != nil {
		return nil, err
	}
	if err := unix.Linkat(dirFd, generationKeyTemp, dirFd, generationKeyFile, 0); err != nil {
		return nil, &os.LinkError{Op: "link", Old: generationKeyTemp, New: generationKeyFile, Err: err}
	}
	// The link is made durable with the directory. Should it be lost to a crash anyway,
	// the next boot creates another key and each marker signed under this one reads as
	// generation 0: one hydrate per server, the safe direction.
	if err := unix.Fsync(dirFd); err != nil {
		return nil, err
	}
	return key, nil
}

// usable reports whether markers can verify under k.
func (k *GenerationKey) usable() bool { return k.key != nil }

// seal returns the marker content recording gen for serverID. Under an unusable key it
// is the bare generation, which no key verifies.
func (k *GenerationKey) seal(serverID string, gen uint64) []byte {
	if !k.usable() {
		return []byte(strconv.FormatUint(gen, 10))
	}
	return fmt.Appendf(nil, "%s %d %x", generationMarkerVersion, gen, k.mac(serverID, gen))
}

// open returns the generation data records for serverID, and false when data is not a
// marker this key signed for serverID.
func (k *GenerationKey) open(serverID string, data []byte) (uint64, bool) {
	if !k.usable() {
		return 0, false
	}
	fields := strings.Fields(string(data))
	if len(fields) != 3 || fields[0] != generationMarkerVersion {
		return 0, false
	}
	gen, err := strconv.ParseUint(fields[1], 10, 64)
	if err != nil {
		return 0, false
	}
	mac, err := hex.DecodeString(fields[2])
	if err != nil {
		return 0, false
	}
	// Constant-time: how long a refusal takes must not tell a forger how much of its
	// MAC was right.
	if !hmac.Equal(mac, k.mac(serverID, gen)) {
		return 0, false
	}
	return gen, true
}

// mac is HMAC-SHA256 over the server id followed by the generation as 8 big-endian
// bytes. The generation is fixed-width, so the input is unambiguous whatever the id's
// length.
func (k *GenerationKey) mac(serverID string, gen uint64) []byte {
	h := hmac.New(sha256.New, k.key)
	h.Write([]byte(serverID))
	h.Write(binary.BigEndian.AppendUint64(nil, gen))
	return h.Sum(nil)
}

// firstUnverified reports whether serverID's marker failing verification has not been
// warned about yet, and records that it now has been.
func (k *GenerationKey) firstUnverified(serverID string) bool {
	k.mu.Lock()
	defer k.mu.Unlock()
	if _, seen := k.warned[serverID]; seen {
		return false
	}
	if k.warned == nil {
		k.warned = map[string]struct{}{}
	}
	k.warned[serverID] = struct{}{}
	k.order = append(k.order, serverID)
	if len(k.order) > maxWarnedUnverified {
		delete(k.warned, k.order[0])
		k.order = k.order[1:]
	}
	return true
}
