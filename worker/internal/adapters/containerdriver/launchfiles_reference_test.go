package containerdriver

import (
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/mmiura-2351/mc-server-dashboard-v2/worker/internal/domain/execution"
)

// referencePathResolver answers for a working set by path, exactly as the launch
// planning did before issue #3352: filepath.Glob for the args file and the legacy
// jar (then in the domain's resolveForgeArgsfile / ResolveLegacyForgeJar) and
// os.Stat for user_jvm_args.txt (then containerPathResolver.Exists). It is
// retained here — in the test only — as the definition of the plan for a working
// set nobody has tampered with: the descriptor-relative lookup that replaced it
// must produce the same plan for every such layout.
func referencePathResolver(workingDir string) execution.PathResolver {
	return execution.PathResolver{
		Resolve: func(rel string) string { return containerWorkDir + "/" + rel },
		Exists: func(rel string) bool {
			_, err := os.Stat(filepath.Join(workingDir, filepath.FromSlash(rel)))
			return err == nil
		},
		Files: func(pattern string) ([]string, error) {
			matches, err := filepath.Glob(filepath.Join(workingDir, filepath.FromSlash(pattern)))
			if err != nil {
				return nil, err
			}
			rels := make([]string, 0, len(matches))
			for _, m := range matches {
				rel, err := filepath.Rel(workingDir, m)
				if err != nil {
					return nil, err
				}
				rels = append(rels, filepath.ToSlash(rel))
			}
			return rels, nil
		},
	}
}

// For every layout a legitimate server can have, the plan — the full command
// line, the installer step, the ambiguity error — and the legacy-jar answer are
// what the path-based planning produced.
func TestLaunchPlanMatchesThePathBasedReference(t *testing.T) {
	const (
		otherArgsRel = forgeVersionsRel + "/1.20.1-47.3.0/unix_args.txt"
		legacyJar    = "forge-1.12.2-14.23.5.2860.jar"
	)
	forge := execution.LaunchModeForgeArgsfile
	layouts := []struct {
		name    string
		mode    execution.LaunchMode
		memory  uint32
		files   []string
		dirs    []string
		missing bool // the working dir itself does not exist
		// what the plan must be, so that agreeing on a wrong answer still fails
		wantInstall []string // the installer step, when Forge is not installed
		warns       bool     // the lookup passes over an entry, and says so
		wantLaunch  []string
		wantErr     error
		wantLegacy  string
	}{
		{name: "vanilla jar", files: []string{"server.jar", "server.properties"},
			wantLaunch: []string{"-jar", "/data/server.jar", "nogui"}},
		{name: "jar with a memory limit", memory: 2048, files: []string{"server.jar"},
			wantLaunch: []string{"-Xms1639M", "-Xmx1639M", "-jar", "/data/server.jar", "nogui"}},
		{name: "jar loader with libraries and a stray args file", files: []string{"server.jar", "libraries/com/example/lib.jar", forgeArgsRel, userJVMArgs},
			wantLaunch: []string{"-jar", "/data/server.jar", "nogui"}},
		{name: "jar with no working dir", missing: true,
			wantLaunch: []string{"-jar", "/data/server.jar", "nogui"}},

		{name: "forge, empty working set", mode: forge, wantInstall: wantInstallArgs},
		{name: "forge, no working dir", mode: forge, missing: true, wantInstall: wantInstallArgs},
		{name: "forge, installer jar only", mode: forge, files: []string{"server.jar", "eula.txt"}, wantInstall: wantInstallArgs},
		{name: "forge, user args but not installed", mode: forge, files: []string{"server.jar", userJVMArgs}, wantInstall: wantInstallArgs},
		{name: "forge, not installed, with a memory limit", mode: forge, memory: 2048, files: []string{"server.jar"},
			wantInstall: []string{"-Xms1639M", "-Xmx1639M", "-jar", "/data/server.jar", "--installServer"}},
		{name: "forge, libraries without the forge tree", mode: forge, files: []string{"libraries/net/minecraft/server/1.20.1/server-1.20.1.jar"}, wantInstall: wantInstallArgs},
		{name: "forge, empty versions directory", mode: forge, dirs: []string{forgeVersionsRel}, wantInstall: wantInstallArgs},
		{name: "forge, version without an args file", mode: forge,
			files: []string{forgeVersionsRel + "/1.20.1-47.2.0/win_args.txt", forgeVersionsRel + "/1.20.1-47.2.0/forge-1.20.1-47.2.0-server.jar"}, wantInstall: wantInstallArgs},
		{name: "forge, a file beside the version directories", mode: forge,
			files: []string{forgeVersionsRel + "/maven-metadata.xml"}, wantInstall: wantInstallArgs, warns: true},

		{name: "forge installed", mode: forge, files: []string{"server.jar", forgeArgsRel},
			wantLaunch: []string{"@/data/" + forgeArgsRel, "nogui"}},
		{name: "forge installed with user args", mode: forge, files: []string{"server.jar", forgeArgsRel, userJVMArgs},
			wantLaunch: []string{"@/data/" + userJVMArgs, "@/data/" + forgeArgsRel, "nogui"}},
		{name: "forge installed with a memory limit", mode: forge, memory: 4096, files: []string{forgeArgsRel, userJVMArgs},
			wantLaunch: []string{"-Xms3277M", "-Xmx3277M", "@/data/" + userJVMArgs, "@/data/" + forgeArgsRel, "nogui"}},
		{name: "forge installed, a second version without an args file", mode: forge,
			files:      []string{forgeArgsRel, forgeVersionsRel + "/1.20.1-47.3.0/win_args.txt"},
			wantLaunch: []string{"@/data/" + forgeArgsRel, "nogui"}},
		{name: "forge installed, legacy jar also present", mode: forge, files: []string{forgeArgsRel, legacyJar},
			wantLaunch: []string{"@/data/" + forgeArgsRel, "nogui"}, wantLegacy: legacyJar},
		{name: "forge, two args files", mode: forge, files: []string{forgeArgsRel, otherArgsRel, userJVMArgs},
			wantErr: execution.ErrForgeArgsfileAmbiguous},

		{name: "legacy forge", mode: forge, files: []string{"server.jar", legacyJar, "forge-installer.jar.log"},
			wantInstall: wantInstallArgs, wantLegacy: legacyJar},
		{name: "legacy forge, two jars", mode: forge, files: []string{legacyJar, "forge-1.12.2-14.23.5.2861.jar"},
			wantInstall: wantInstallArgs, wantErr: execution.ErrLegacyForgeJarAmbiguous},
		{name: "legacy forge, jar only below the root", mode: forge, files: []string{"libraries/" + legacyJar, "mods/forge-addon.jar"},
			wantInstall: wantInstallArgs},
	}

	for _, l := range layouts {
		t.Run(l.name, func(t *testing.T) {
			dir := t.TempDir()
			if l.missing {
				dir = filepath.Join(dir, "absent")
			}
			for _, f := range l.files {
				plant(t, filepath.Join(dir, filepath.FromSlash(f)), "x")
			}
			for _, d := range l.dirs {
				mkdir(t, filepath.Join(dir, filepath.FromSlash(d)))
			}
			spec := forgeSpec(dir)
			spec.LaunchMode = l.mode
			spec.MemoryLimitMB = l.memory
			logged := &syncBuffer{}
			reference := referencePathResolver(dir)
			actual := containerPathResolver(dir, slog.New(slog.NewTextHandler(logged, nil)))

			wantPlan, wantPlanErr := execution.BuildLaunchPlan(spec, reference)
			plan, planErr := execution.BuildLaunchPlan(spec, actual)
			if !reflect.DeepEqual(plan, wantPlan) || errString(planErr) != errString(wantPlanErr) {
				t.Fatalf("plan = %+v, err = %v; the path-based reference gives %+v, err = %v", plan, planErr, wantPlan, wantPlanErr)
			}
			wantRel, wantFound, wantLegacyErr := execution.ResolveLegacyForgeJar(reference)
			rel, found, legacyErr := execution.ResolveLegacyForgeJar(actual)
			if rel != wantRel || found != wantFound || errString(legacyErr) != errString(wantLegacyErr) {
				t.Fatalf("legacy jar = %q, %v, err = %v; the path-based reference gives %q, %v, err = %v",
					rel, found, legacyErr, wantRel, wantFound, wantLegacyErr)
			}

			// The reference is not vacuous: both agree on the answer this layout must have.
			planAmbiguous := errors.Is(l.wantErr, execution.ErrForgeArgsfileAmbiguous)
			legacyAmbiguous := errors.Is(l.wantErr, execution.ErrLegacyForgeJarAmbiguous)
			if errors.Is(planErr, execution.ErrForgeArgsfileAmbiguous) != planAmbiguous ||
				errors.Is(legacyErr, execution.ErrLegacyForgeJarAmbiguous) != legacyAmbiguous {
				t.Fatalf("plan err = %v, legacy err = %v; want %v", planErr, legacyErr, l.wantErr)
			}
			if !planAmbiguous {
				want := execution.LaunchPlan{LaunchArgs: l.wantLaunch}
				if l.wantInstall != nil {
					want = execution.LaunchPlan{NeedsInstall: true, InstallArgs: l.wantInstall}
				}
				if !reflect.DeepEqual(plan, want) {
					t.Fatalf("plan = %+v, want %+v", plan, want)
				}
			}
			if rel != l.wantLegacy || found != (l.wantLegacy != "") {
				t.Fatalf("legacy jar = %q, found = %v; want %q", rel, found, l.wantLegacy)
			}
			// The lookup warns only about an entry it passed over: here, a plain file
			// where a version directory would be.
			if (logged.String() != "") != l.warns {
				t.Fatalf("worker log = %q, want a warning only for an entry passed over (%v)", logged.String(), l.warns)
			}
		})
	}
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}
