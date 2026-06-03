# Release Policy

Versioning and release conventions for Minecraft Server Dashboard v2. This
document fixes the *policy*; the release **automation** (tooling, the version
source-of-truth file, release-notes generation) is set up as the toolchain lands
and is marked *(forthcoming)* below.

> **One version for the whole monorepo.** `api/`, `worker/`, and `proto/` ship
> together and are kept in lock-step (see [`CONTRIBUTING.md`](CONTRIBUTING.md)
> and [`../REQUIREMENTS.md`](../REQUIREMENTS.md)). They therefore share **one
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
the previous tag — grouped by label (features / fixes / breaking / …) and with
noise (e.g. dependency or release-automation PRs) excluded. The grouping and
exclusion rules live in repository release configuration *(forthcoming)*.

A curated `CHANGELOG.md` may be reintroduced if external consumers appear or at
`1.0.0`. Until then, the generated notes on each GitHub Release are the
changelog.

## 4. Release flow (policy)

1. Develop and merge PRs normally; each PR has a clear imperative title and a
   category label, and a release-affecting change signals the intended bump
   (MAJOR / MINOR / PATCH).
2. Cut a release by: bumping the repository-wide version, tagging `vX.Y.Z`, and
   publishing the GitHub Release with notes generated from the PRs merged since
   the previous tag (Section 3).
3. A release builds and publishes both components (`api/`, `worker/`) from the
   same tagged commit, so the `api/` and `worker/` artifacts of a release come
   from one source revision.

The concrete mechanics — where the version number is stored, whether bumping and
tagging are automated, and how the GitHub Release is published — are
*(forthcoming)* and will be documented here once the release tooling is chosen.

## 5. Hotfix

For a critical bug in a released version, a hotfix branched from the release tag
is acceptable instead of the normal flow from the main branch: branch from
`vX.Y.Z`, commit the fix, release as `vX.Y.(Z+1)`, and merge the fix back into
the main line.

## 6. Open decisions

- **Version source-of-truth** in the monorepo (a top-level version file vs. the
  git tag as the authority, and how `api/` and `worker/` read it at build time).
- **Release automation** tool, and the release-notes grouping/exclusion config.
