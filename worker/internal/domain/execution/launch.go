package execution

import (
	"errors"
	"fmt"
	"path/filepath"
)

// ForgeInstallLogRelpath is the working-dir-relative path the supervised Forge
// installer's combined output is written to, so an operator can read it through
// the files API (issue #305). It lives under logs/ alongside the server's own
// logs.
const ForgeInstallLogRelpath = "logs/forge-install.log"

// forgeArgsfileGlob matches the Forge launcher's generated unix args file under
// the working set. Forge writes it to a version-stamped directory, so the
// version segment is globbed and the single match is expected at launch time.
const forgeArgsfileGlob = "libraries/net/minecraftforge/forge/*/unix_args.txt"

// forgeUserJVMArgs is the optional user JVM args file the Forge launch reads
// before the generated args file; Forge ships it next to the server on install.
const forgeUserJVMArgs = "user_jvm_args.txt"

// ErrForgeArgsfileAmbiguous is returned when more than one Forge args file
// matches the glob in a working set: the launch cannot pick one deterministically
// (a corrupt / double-installed working set). The driver surfaces it as a start
// failure rather than guessing.
var ErrForgeArgsfileAmbiguous = errors.New("execution: multiple Forge args files found")

// ErrLegacyForgeJarAmbiguous is returned when more than one legacy Forge launch
// jar (forge-*.jar) is found in the working set root after install: the launch
// cannot pick one deterministically.
var ErrLegacyForgeJarAmbiguous = errors.New("execution: multiple legacy Forge jars found")

// legacyForgeJarGlob matches the legacy Forge universal/launch jar in the
// working set root. Legacy Forge installers (MC <=1.16.x) produce a
// forge-<mc_version>-<forge_version>.jar instead of unix_args.txt.
const legacyForgeJarGlob = "forge-*.jar"

// LaunchPlan describes how to launch (or first install) a server, resolved from
// the spec and the working set's current contents (issue #305). When NeedsInstall
// is true the driver runs InstallArgs to completion, then re-plans (the args file
// is then present and LaunchArgs is built); otherwise LaunchArgs launches the
// server directly.
type LaunchPlan struct {
	// NeedsInstall is true when the Forge args file is absent, so the working set
	// is uninstalled and the supervised installer must run first.
	NeedsInstall bool
	// InstallArgs is the JVM argument vector (after the java binary) for the
	// supervised Forge installer: `[heap] -jar <jar> --installServer`. Set only
	// when NeedsInstall is true.
	InstallArgs []string
	// LaunchArgs is the JVM argument vector (after the java binary) for the server
	// launch. Set only when NeedsInstall is false.
	LaunchArgs []string
}

// PathResolver maps a working-dir-relative path to the path a driver passes on
// the launch command line: a host-absolute path for the host-process driver, the
// in-container path for the container driver. relExists reports whether the
// relative path exists in the working set (the driver checks the host path).
type PathResolver struct {
	// Resolve maps a slash-separated working-dir-relative path to the driver's
	// command-line path.
	Resolve func(relpath string) string
	// Exists reports whether the working-dir-relative path is present in the
	// working set (checked against the host working dir).
	Exists func(relpath string) bool
}

// BuildLaunchPlan resolves how to launch the server described by spec, given the
// host workingDir (for globbing the working set) and a PathResolver mapping
// working-set paths onto the driver's command line (issue #305). For
// LaunchModeJar it returns the historical JAR launch. For LaunchModeForgeArgsfile
// it globs the Forge args file: present -> a Forge args-file launch; absent ->
// NeedsInstall with the installer args; ambiguous -> an error.
func BuildLaunchPlan(spec InstanceSpec, workingDir string, paths PathResolver) (LaunchPlan, error) {
	if spec.LaunchMode == LaunchModeForgeArgsfile {
		return forgePlan(spec, workingDir, paths)
	}
	return LaunchPlan{LaunchArgs: jarLaunchArgs(spec, paths.Resolve(spec.JarRelpath))}, nil
}

// forgePlan builds the Forge launch plan: an args-file launch when exactly one
// args file is present, the installer step when none is, an error when several.
func forgePlan(spec InstanceSpec, workingDir string, paths PathResolver) (LaunchPlan, error) {
	rel, found, err := resolveForgeArgsfile(workingDir)
	if err != nil {
		return LaunchPlan{}, err
	}
	if !found {
		return LaunchPlan{NeedsInstall: true, InstallArgs: forgeInstallArgs(spec, paths.Resolve(spec.JarRelpath))}, nil
	}
	jvmArgsPath := ""
	if paths.Exists(forgeUserJVMArgs) {
		jvmArgsPath = paths.Resolve(forgeUserJVMArgs)
	}
	return LaunchPlan{LaunchArgs: forgeLaunchArgs(spec, jvmArgsPath, paths.Resolve(rel))}, nil
}

// heapHeadroomMB returns the memory (MiB) to reserve below the memory LIMIT for
// JVM off-heap + native overhead (metaspace, thread stacks, code cache, GC
// structures, direct/mapped buffers), so the derived heap plus that overhead
// stays under the ceiling and the kernel/Docker (set later by #707/#708) does not
// OOM-kill the process. The reserve is max(20% of the limit, 256 MiB): the 20%
// scales with the heap (off-heap/native cost grows with workload and heap size),
// while the 256 MiB floor covers the fixed JVM base for a small server where 20%
// would be too thin. (The API floors the limit at 512 MiB, #705, so the derived
// heap is comfortably positive for any accepted limit.)
func heapHeadroomMB(limitMB uint32) uint32 {
	headroom := limitMB / 5
	if headroom < 256 {
		headroom = 256
	}
	return headroom
}

// heapArgs derives the JVM heap (-Xms/-Xmx) from the spec's memory LIMIT, or nil
// when unset (limit 0 -> the driver/JVM picks a default, the pre-#706 launch).
// -Xmx is the limit minus headroom (see heapHeadroomMB); -Xms is pinned equal to
// -Xmx so the JVM commits its full heap up front rather than growing under load,
// which keeps a long-running server's footprint predictable against the ceiling.
// A limit so small that the headroom would consume the whole heap yields no flags
// (the JVM default is safer than a non-positive -Xmx).
func heapArgs(spec InstanceSpec) []string {
	if spec.MemoryLimitMB == 0 {
		return nil
	}
	headroom := heapHeadroomMB(spec.MemoryLimitMB)
	if headroom >= spec.MemoryLimitMB {
		return nil
	}
	heap := fmt.Sprintf("%dM", spec.MemoryLimitMB-headroom)
	return []string{"-Xms" + heap, "-Xmx" + heap}
}

// jarLaunchArgs builds the historical JAR launch: `[heap] -jar <jarPath> nogui`.
func jarLaunchArgs(spec InstanceSpec, jarPath string) []string {
	args := heapArgs(spec)
	return append(args, "-jar", jarPath, "nogui")
}

// forgeInstallArgs builds the supervised Forge installer:
// `[heap] -jar <jarPath> --installServer`.
func forgeInstallArgs(spec InstanceSpec, jarPath string) []string {
	args := heapArgs(spec)
	return append(args, "-jar", jarPath, "--installServer")
}

// forgeLaunchArgs builds the Forge args-file launch:
// `[heap] @user_jvm_args.txt @<argsfile> nogui`. The user JVM args file is
// included only when present so a working set without it still launches.
func forgeLaunchArgs(spec InstanceSpec, jvmArgsPath, argsPath string) []string {
	args := heapArgs(spec)
	if jvmArgsPath != "" {
		args = append(args, "@"+jvmArgsPath)
	}
	return append(args, "@"+argsPath, "nogui")
}

// JarLaunchArgs builds the historical JAR launch args: `[heap] -jar <jarPath> nogui`.
// Exported for use by the container driver's legacy Forge fallback path.
func JarLaunchArgs(spec InstanceSpec, jarPath string) []string {
	return jarLaunchArgs(spec, jarPath)
}

// ResolveLegacyForgeJar globs for a legacy Forge launch jar (forge-*.jar) in
// the working set root. Legacy Forge installers (MC <=1.16.x) produce this jar
// instead of unix_args.txt. Returns the working-dir-relative slash path when
// exactly one match exists. Returns ("", false, nil) when none is found, and
// ErrLegacyForgeJarAmbiguous when multiple matches exist.
func ResolveLegacyForgeJar(workingDir string) (relpath string, found bool, err error) {
	matches, err := filepath.Glob(filepath.Join(workingDir, legacyForgeJarGlob))
	if err != nil {
		return "", false, fmt.Errorf("execution: glob legacy Forge jar: %w", err)
	}
	switch len(matches) {
	case 0:
		return "", false, nil
	case 1:
		rel, err := filepath.Rel(workingDir, matches[0])
		if err != nil {
			return "", false, fmt.Errorf("execution: relativize legacy Forge jar: %w", err)
		}
		return filepath.ToSlash(rel), true, nil
	default:
		return "", false, fmt.Errorf("%w: %d matching %s", ErrLegacyForgeJarAmbiguous, len(matches), legacyForgeJarGlob)
	}
}

// resolveForgeArgsfile globs the Forge args file under workingDir, reporting
// whether exactly one match exists. It returns ("", false, nil) when no match
// exists (install needed), the single match's working-dir-relative slash path
// with found=true when exactly one exists, and ErrForgeArgsfileAmbiguous when
// more than one matches.
func resolveForgeArgsfile(workingDir string) (relpath string, found bool, err error) {
	matches, err := filepath.Glob(filepath.Join(workingDir, filepath.FromSlash(forgeArgsfileGlob)))
	if err != nil {
		// The only error filepath.Glob returns is ErrBadPattern; the pattern is a
		// constant, so this never fires in practice. Surface it rather than hide it.
		return "", false, fmt.Errorf("execution: glob Forge args file: %w", err)
	}
	switch len(matches) {
	case 0:
		return "", false, nil
	case 1:
		rel, err := filepath.Rel(workingDir, matches[0])
		if err != nil {
			return "", false, fmt.Errorf("execution: relativize Forge args file: %w", err)
		}
		return filepath.ToSlash(rel), true, nil
	default:
		return "", false, fmt.Errorf("%w: %d under %s", ErrForgeArgsfileAmbiguous, len(matches), forgeArgsfileGlob)
	}
}
