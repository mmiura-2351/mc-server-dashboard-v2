package execution

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// ForgeInstallLogRelpath is the working-dir-relative path the supervised Forge installer's combined output is
// written to, so an operator can read it through the files API. It lives under logs/ alongside the server's own
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

// Legacy Forge produces forge-*.jar; the installer must remain server.jar so it cannot match this glob.
const legacyForgeJarGlob = "forge-*.jar"

// LaunchPlan requires InstallArgs followed by re-planning when NeedsInstall is true; otherwise LaunchArgs is
// ready.
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
// the launch command line: a host-absolute path for the now-removed host-process driver, the
// in-container path for the container driver. Exists reports whether the
// relative path exists in the working set (the driver checks the host path).
type PathResolver struct {
	// Resolve maps a slash-separated working-dir-relative path to the driver's
	// command-line path.
	Resolve func(relpath string) string
	// Exists reports whether the working-dir-relative path is present in the
	// working set (checked against the host working dir).
	Exists func(relpath string) bool
}

// BuildLaunchPlan returns a JAR launch, a unique Forge launch artifact, or an install plan when absent.
// Multiple matching artifacts are an error; paths maps host paths to driver paths.
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

// Reserve max(20%, 256 MiB) for JVM native and off-heap memory below the total limit.
func heapHeadroomMB(limitMB uint32) uint32 {
	headroom := limitMB / 5
	if headroom < 256 {
		headroom = 256
	}
	return headroom
}

// Pin Xms and Xmx to the limit minus headroom for predictable heap commitment.
// Unset or too-small limits leave JVM defaults.
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

// ResolveLegacyForgeJar finds legacy Forge launch JARs when installers do not produce args files.
// Return the unique relative path, no match, or ErrLegacyForgeJarAmbiguous.
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

// CleanForgeInstallArtifacts removes Forge args files and launch JARs, preserving server.jar and
// user_jvm_args.txt.
// Only glob errors are returned; individual removal failures are ignored.
func CleanForgeInstallArtifacts(workingDir string) error {
	for _, glob := range []string{
		filepath.Join(workingDir, filepath.FromSlash(forgeArgsfileGlob)),
		filepath.Join(workingDir, legacyForgeJarGlob),
	} {
		matches, err := filepath.Glob(glob)
		if err != nil {
			return fmt.Errorf("execution: clean Forge artifacts: glob %s: %w", glob, err)
		}
		for _, m := range matches {
			_ = os.Remove(m)
		}
	}
	return nil
}

// resolveForgeArgsfile returns the unique relative match, no match when install is needed, or an ambiguity
// error.
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
