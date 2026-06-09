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

// JAR mode with a memory limit derives the JVM heap (-Xms/-Xmx = limit minus
// headroom) and keeps the flags before the jar (issue #706). At a 2048 MiB limit
// the headroom is max(20%, 256) = 409 MiB, so the heap is 1639 MiB.
func TestBuildLaunchPlanJarHeap(t *testing.T) {
	dir := t.TempDir()
	spec := InstanceSpec{ServerID: "s1", WorkingDir: dir, JarRelpath: "server.jar", MemoryLimitMB: 2048}

	plan, err := BuildLaunchPlan(spec, dir, hostPaths(dir))
	if err != nil {
		t.Fatalf("BuildLaunchPlan: %v", err)
	}
	want := []string{"-Xms1639M", "-Xmx1639M", "-jar", filepath.Join(dir, "server.jar"), "nogui"}
	if !equalArgs(plan.LaunchArgs, want) {
		t.Fatalf("LaunchArgs = %v, want %v", plan.LaunchArgs, want)
	}
}

// heapArgs derivation: unset limit emits no flags; the 256 MiB headroom floor
// applies at the small (512 MiB) end; the 20% headroom applies once it exceeds
// the floor (at 2048 MiB).
func TestHeapArgsFromMemoryLimit(t *testing.T) {
	cases := []struct {
		name    string
		limitMB uint32
		want    []string
	}{
		{name: "unset emits nothing", limitMB: 0, want: nil},
		// 512 floor (#705): headroom = max(102, 256) = 256 -> heap 256.
		{name: "floor uses 256 headroom floor", limitMB: 512, want: []string{"-Xms256M", "-Xmx256M"}},
		// boundary where 20% overtakes the 256 floor: 1280/5 = 256.
		{name: "boundary 20pct equals floor", limitMB: 1280, want: []string{"-Xms1024M", "-Xmx1024M"}},
		// 8192: headroom = max(1638, 256) = 1638 -> heap 6554.
		{name: "large uses 20pct headroom", limitMB: 8192, want: []string{"-Xms6554M", "-Xmx6554M"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := heapArgs(InstanceSpec{MemoryLimitMB: tc.limitMB})
			if !equalArgs(got, tc.want) {
				t.Fatalf("heapArgs(%d) = %v, want %v", tc.limitMB, got, tc.want)
			}
		})
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
