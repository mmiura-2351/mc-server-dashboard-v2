package execution

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// hostPaths is a PathResolver that maps relpaths onto the host working dir,
// mirroring the host-process driver, and checks existence against it.
func hostPaths(workingDir string) PathResolver {
	return PathResolver{
		Resolve: func(rel string) string { return filepath.Join(workingDir, filepath.FromSlash(rel)) },
		Exists: func(rel string) bool {
			_, err := os.Stat(filepath.Join(workingDir, filepath.FromSlash(rel)))
			return err == nil
		},
	}
}

func writeFile(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(path, []byte("x"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
}

// JAR mode (and the zero value) produces the historical `-jar <jar> nogui`
// launch, byte-for-byte, and never needs an install.
func TestBuildLaunchPlanJar(t *testing.T) {
	dir := t.TempDir()
	spec := InstanceSpec{ServerID: "s1", WorkingDir: dir, JarRelpath: "server.jar"}

	plan, err := BuildLaunchPlan(spec, dir, hostPaths(dir))
	if err != nil {
		t.Fatalf("BuildLaunchPlan: %v", err)
	}
	if plan.NeedsInstall {
		t.Fatal("JAR launch must never need install")
	}
	want := []string{"-jar", filepath.Join(dir, "server.jar"), "nogui"}
	if !equalArgs(plan.LaunchArgs, want) {
		t.Fatalf("LaunchArgs = %v, want %v", plan.LaunchArgs, want)
	}
}

// JAR mode with a heap size keeps the -Xms/-Xmx flags before the jar.
func TestBuildLaunchPlanJarHeap(t *testing.T) {
	dir := t.TempDir()
	spec := InstanceSpec{ServerID: "s1", WorkingDir: dir, JarRelpath: "server.jar", MemoryMB: 2048}

	plan, err := BuildLaunchPlan(spec, dir, hostPaths(dir))
	if err != nil {
		t.Fatalf("BuildLaunchPlan: %v", err)
	}
	want := []string{"-Xms2048M", "-Xmx2048M", "-jar", filepath.Join(dir, "server.jar"), "nogui"}
	if !equalArgs(plan.LaunchArgs, want) {
		t.Fatalf("LaunchArgs = %v, want %v", plan.LaunchArgs, want)
	}
}

// Forge mode with no args file in the working set needs the supervised install
// step: `-jar <installer> --installServer`.
func TestBuildLaunchPlanForgeNeedsInstall(t *testing.T) {
	dir := t.TempDir()
	spec := InstanceSpec{ServerID: "s1", WorkingDir: dir, JarRelpath: "server.jar", LaunchMode: LaunchModeForgeArgsfile}

	plan, err := BuildLaunchPlan(spec, dir, hostPaths(dir))
	if err != nil {
		t.Fatalf("BuildLaunchPlan: %v", err)
	}
	if !plan.NeedsInstall {
		t.Fatal("Forge with no args file must need install")
	}
	want := []string{"-jar", filepath.Join(dir, "server.jar"), "--installServer"}
	if !equalArgs(plan.InstallArgs, want) {
		t.Fatalf("InstallArgs = %v, want %v", plan.InstallArgs, want)
	}
}

// Forge mode with exactly one args file present launches via that args file. The
// optional user_jvm_args.txt is included only when present.
func TestBuildLaunchPlanForgeLaunch(t *testing.T) {
	dir := t.TempDir()
	argsRel := "libraries/net/minecraftforge/forge/1.20.1-47.2.0/unix_args.txt"
	writeFile(t, filepath.Join(dir, argsRel))
	writeFile(t, filepath.Join(dir, "user_jvm_args.txt"))
	spec := InstanceSpec{ServerID: "s1", WorkingDir: dir, JarRelpath: "server.jar", LaunchMode: LaunchModeForgeArgsfile}

	plan, err := BuildLaunchPlan(spec, dir, hostPaths(dir))
	if err != nil {
		t.Fatalf("BuildLaunchPlan: %v", err)
	}
	if plan.NeedsInstall {
		t.Fatal("Forge with an args file present must not need install")
	}
	want := []string{
		"@" + filepath.Join(dir, "user_jvm_args.txt"),
		"@" + filepath.Join(dir, filepath.FromSlash(argsRel)),
		"nogui",
	}
	if !equalArgs(plan.LaunchArgs, want) {
		t.Fatalf("LaunchArgs = %v, want %v", plan.LaunchArgs, want)
	}
}

// Forge launch omits the user JVM args file when it is absent.
func TestBuildLaunchPlanForgeLaunchNoUserArgs(t *testing.T) {
	dir := t.TempDir()
	argsRel := "libraries/net/minecraftforge/forge/1.20.1-47.2.0/unix_args.txt"
	writeFile(t, filepath.Join(dir, argsRel))
	spec := InstanceSpec{ServerID: "s1", WorkingDir: dir, JarRelpath: "server.jar", LaunchMode: LaunchModeForgeArgsfile}

	plan, err := BuildLaunchPlan(spec, dir, hostPaths(dir))
	if err != nil {
		t.Fatalf("BuildLaunchPlan: %v", err)
	}
	for _, a := range plan.LaunchArgs {
		if strings.Contains(a, "user_jvm_args.txt") {
			t.Fatalf("LaunchArgs = %v, want no user_jvm_args when absent", plan.LaunchArgs)
		}
	}
	if plan.LaunchArgs[0] != "@"+filepath.Join(dir, filepath.FromSlash(argsRel)) {
		t.Fatalf("LaunchArgs[0] = %q, want the args file", plan.LaunchArgs[0])
	}
}

// More than one args file is ambiguous: the plan errors rather than guessing.
func TestBuildLaunchPlanForgeAmbiguous(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "libraries/net/minecraftforge/forge/1.20.1-47.2.0/unix_args.txt"))
	writeFile(t, filepath.Join(dir, "libraries/net/minecraftforge/forge/1.20.1-47.3.0/unix_args.txt"))
	spec := InstanceSpec{ServerID: "s1", WorkingDir: dir, JarRelpath: "server.jar", LaunchMode: LaunchModeForgeArgsfile}

	_, err := BuildLaunchPlan(spec, dir, hostPaths(dir))
	if !errors.Is(err, ErrForgeArgsfileAmbiguous) {
		t.Fatalf("err = %v, want ErrForgeArgsfileAmbiguous", err)
	}
}

func equalArgs(got, want []string) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}
