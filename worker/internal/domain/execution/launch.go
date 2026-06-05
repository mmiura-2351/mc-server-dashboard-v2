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

// heapArgs returns the -Xms/-Xmx flags for the spec's MemoryMB, or nil when
// unset (the driver/JVM picks a default).
func heapArgs(spec InstanceSpec) []string {
	if spec.MemoryMB == 0 {
		return nil
	}
	heap := fmt.Sprintf("%dM", spec.MemoryMB)
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
