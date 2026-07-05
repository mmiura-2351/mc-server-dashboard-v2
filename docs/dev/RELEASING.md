# Release Policy

Versioning and release conventions for Minecraft Server Dashboard v2. This
document fixes the *policy*. The version source-of-truth, release-notes
generation, and the tag-driven release workflow are in place (Sections 3 and 4).
Building and publishing deployable artifacts stays aspirational until a
packaging/deployment design exists, and is marked *(forthcoming)* below.

> **One version for the whole monorepo.** `api/`, `worker/`, `relay/`, `webui/`,
> and `proto/` ship together and are kept in lock-step (see
> [`CONTRIBUTING.md`](CONTRIBUTING.md) and
> [`../REQUIREMENTS.md`](../REQUIREMENTS.md)). They therefore share **one
> repository-wide SemVer version**, not per-component versions.

## 1. Versioning (SemVer)

Versions follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html)
as `MAJOR.MINOR.PATCH`:

| Part | Meaning |
|---|---|
| MAJOR | Backwards-incompatible change (HTTP API, the `proto/` contract, or operator-visible behavior) |
| MINOR | Backwards-compatible new functionality |
| PATCH | Backwards-compatible bug fix |

Because the API↔Worker contract lives in `proto/`, a **breaking `proto/`
change is a backwards-incompatible change** and is versioned accordingly.

### 1.1 During `0.x.y`

While the public surface is unstable (`0.x.y`):

- **Backwards-incompatible change** → bump MINOR (`0.1.0` → `0.2.0`).
- **New functionality or compatible bug fix** → bump PATCH (`0.1.0` → `0.1.1`).

Reaching `1.0.0` is decided separately, when the API and `proto/` contract are
judged stable enough for production use.

### 1.2 Milestones vs versions

The milestone labels from `REQUIREMENTS.md` (**M1**, M2, …) describe *scope*,
not release numbers. A milestone may span several releases. Do not encode "M1"
into a version string; use SemVer numbers for releases.

## 2. Tag naming

- Releases: `vX.Y.Z` (the leading `v` is required).
- Pre-releases: `vX.Y.Z-rc.N` (e.g. `v0.2.0-rc.1`). Other suffixes are not used
  by default.
- Any non-release tag (local verification, etc.) must **not** carry the `v`
  prefix.

## 3. Release notes (generated)

v2 does **not** keep a hand-maintained `CHANGELOG.md`. Release notes are
generated from the pull requests merged into a release, using their titles and
labels. This avoids the toil and the merge conflicts of a shared `[Unreleased]`
section, and it stays accurate because it derives from the merge history.

It works because of two conventions the project already follows:

- PRs are **squash-merged**, so each release's history is one commit per PR with
  the PR title as the subject.
- PR titles are short and imperative, and each PR carries a category label.

At release time, the notes for `vX.Y.Z` are produced from the PRs merged since
the previous tag — grouped by label (Breaking Changes / Features / Bug Fixes /
Documentation, with anything else under Other Changes) and with noise (the
`dependencies` and `chore` labels) excluded. The grouping and exclusion rules
live in [`.github/release.yml`](../../.github/release.yml); the labels driving
them are defined in [`CONTRIBUTING.md`](CONTRIBUTING.md) Section 5.

A curated `CHANGELOG.md` may be reintroduced if external consumers appear or at
`1.0.0`. Until then, the generated notes on each GitHub Release are the
changelog.

## 4. Release flow

### 4.1 Version source of truth: the git tag

**The git tag is the single authority for the version.** No version file tracks
the release and none is bumped per release — the `vX.Y.Z` tag on a commit *is*
the release version. The only checked-in `version` fields, `api/pyproject.toml`
and `webui/package.json`, are held at a frozen `0.0.0`: their manifests carry a
`version` field by convention (PEP 621 requires it; npm's manifest expects it),
but neither is a release version — neither is read at runtime and both are
intentionally never bumped. This is the simplest correct choice at this stage:
one repository-wide SemVer (per the monorepo note above), releases cut by tag
push (Section 4.3), and nothing that can drift out of step.

Both `worker/cmd/worker/main.go` and `relay/cmd/relay/main.go` declare
`var version = "0.0.0-dev"`, overridden at build time via
`go build -ldflags "-X main.version=<tag>"`. The Dockerfiles accept a
`VERSION` build arg (defaulting to `0.0.0-dev`), and `make build`,
`scripts/update.sh`, and `scripts/deploy.sh` pass
`git describe --tags --always` as that arg. A plain `go build` without
`-ldflags` (or a compose build without the arg) keeps the `0.0.0-dev`
fallback, which is correct for local development.

**Alternatives considered and rejected:**

- *A top-level `VERSION` file as the authority.* Adds a bump-and-commit step
  before every tag and a second place that can disagree with the tag. It buys
  nothing while there are no build artifacts that must read a version offline.
- *Per-module versions.* Rejected by policy: the components ship in lock-step
  and share one repository-wide version (see the monorepo note above).

### 4.2 Release flow (policy)

1. Develop and merge PRs normally; each PR has a clear imperative title and a
   category label, and a release-affecting change signals the intended bump
   (MAJOR / MINOR / PATCH).
2. Cut a release by tagging `vX.Y.Z` on a green `main` commit and pushing the
   tag; the release workflow publishes the GitHub Release with notes generated
   from the PRs merged since the previous tag (Section 3).
3. *(forthcoming)* A release builds and publishes all components (`api/`,
   `worker/`, `relay/`, `webui/`) from the same tagged commit, so the artifacts
   of a release come from one source revision. All runtime components now have
   Dockerfiles and a single-host compose stack
   ([`DEPLOYMENT.md`](DEPLOYMENT.md)), but no registry-publish target exists yet;
   the workflow in Section 4.3 only publishes the GitHub Release.

### 4.3 Cutting a release (operator steps)

The release is published by
[`.github/workflows/release.yml`](../../.github/workflows/release.yml), which
runs on push of any `v*` tag. To cut a release:

1. Pick the commit to release — normally the latest `main` — and confirm its CI
   is green.
2. Tag it (annotated) and push the tag:

   ```sh
   git fetch origin
   git tag -a vX.Y.Z -m "vX.Y.Z" origin/main      # or a specific <sha>
   git push origin vX.Y.Z
   ```

3. The workflow then, for that tag:
   - **validates** the tag is well-formed SemVer (`vX.Y.Z` or `vX.Y.Z-rc.N`,
     no leading zeros) and **fails** the release if it is not;
   - **checks monotonicity** — the tag must sort strictly above the highest
     existing release tag (the first-ever release is allowed);
   - **creates the GitHub Release** with `gh release create --generate-notes`
     (so the notes use [`.github/release.yml`](../../.github/release.yml)),
     marking any `-rc.N` tag as a GitHub pre-release.

   No manual step in the GitHub UI is needed. If validation fails, delete the
   bad tag (`git push origin :refs/tags/vX.Y.Z`) and push a corrected one.

## 5. Hotfix

For a critical bug in a released version, a hotfix branched from the release tag
is acceptable instead of the normal flow from the main branch: branch from
`vX.Y.Z`, commit the fix, release as `vX.Y.(Z+1)`, and merge the fix back into
the main line.

## 6. Open decisions

- **Artifact build/publish** for `api/` and `worker/` (Section 4.2 item 3):
  deferred until a packaging/deployment design exists.
- **Automated version bumping / PR-driven release cutting:** not adopted —
  manual tag push (Section 4.3) is the flow for now.

Resolved: the **version source of truth** is the git tag (Section 4.1), and the
**release automation** is the tag-driven workflow (Section 4.3) plus the
release-notes config (Section 3).
