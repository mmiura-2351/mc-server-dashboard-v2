# Dependency Policy

How v2 takes on and updates third-party dependencies. This document fixes the
*policy*; the concrete mechanisms (lockfile commands, automated-updater
configuration, the cooldown enforcement per tool) are set up as the toolchain
lands and are marked *(forthcoming)*.

> **Two ecosystems.** `api/` is Python; `worker/` and `relay/` are Go. The
> policy below applies to both; where a rule is expressed per-tool, each
> ecosystem's section states its own form.

## 1. Pinning style

- **Direct runtime dependencies** are constrained so compatible updates flow in
  but a **major (breaking) bump is always explicit and PR-reviewed**.
  - Python (`api/`): a version range that admits minor/patch and caps the next
    major (for a `0.x` library, cap the next minor, since `0.x` may break on a
    minor bump).
  - Go (`worker/`, `relay/`): a major version is part of the module import
    path, so a major bump is inherently a separate, explicit change;
    minor/patch updates are selected by the module graph.
- **Dev / tooling dependencies** (linters, test runners, type checkers, build
  helpers) are kept current with lower-bound-style ranges that still **cap the
  next major** (e.g. `mypy-protobuf<6`, `pytest-asyncio<2`, `pytest-timeout<3`):
  staying current is cheap, but a major bump is reviewed explicitly like any
  other. A range may carry a tighter **temporary cap** when the 7-day cooldown
  (Section 3) excludes the newest release; that cap is lifted once the release
  ages out of the cooldown window.
- **Transitive dependencies** are not declared by hand; they are pinned in the
  lockfile, which is the single source of truth.

## 2. Lockfiles

- Each ecosystem commits its lockfile (`api/`: the uv lockfile; `worker/` and
  `relay/`: `go.sum` with `go.mod`). Lockfiles are the reproducibility boundary
  and must be committed. (`go.sum` is generated, and from then on committed,
  only once the module has external dependencies; a dependency-free module has
  only `go.mod`.)
- Reproducible installs resolve from the lockfile; routine updates regenerate it
  through the ecosystem's own command *(forthcoming)*.

## 3. Supply-chain cooldown

To mitigate maintainer-account takeover, typosquatting, and compromised
releases — which can take several days to detect and retract — **do not adopt
any release published within the last 7 days**. Holding a 7-day window absorbs
most public-incident timelines.

- The cooldown applies to **both** ecosystems.
- It is enforced at dependency-resolution and at the automated-update layer; the
  exact mechanism is per-tool *(forthcoming)* (e.g. an "exclude releases newer
  than N days" resolver option for `api/`, and the automated-updater's cooldown
  setting for both).
- **Security updates bypass the cooldown** (see Section 4); a known-exploited
  vulnerability outweighs the supply-chain risk window.

When a cooldown bypass is required outside a security update, document the reason
(advisory link / rationale) in the PR.

## 4. Security updates

| Trigger | Response |
|---|---|
| Security advisory affecting a dependency | Open a patch PR within **1 week**. |
| Automated security-update PR | Triage within **1 business day**. |
| High-severity (RCE, auth bypass, etc.) | Patch out-of-band, outside the normal cadence. |

Security work is labeled so it is easy to find, and — as above — is exempt from
the cooldown.

## 5. Exact pinning (exception)

Pin an exact version only when one of these holds, and add a comment above the
pin explaining why and linking the advisory/issue:

- A security requirement mandates an exact version.
- A known incompatibility prevents any other version.
- The upstream has a record of breaking on patch bumps.

## 6. Automated updates

Dependabot runs weekly (Monday) for every ecosystem in the repository. The
configuration lives in `.github/dependabot.yml` and covers:

| Ecosystem | Directory | What it covers |
|---|---|---|
| `pip` | `/api` | Python runtime + dev dependencies |
| `gomod` | `/worker` | Go worker module |
| `gomod` | `/relay` | Go relay module |
| `npm` | `/webui` | React frontend |
| `github-actions` | `/` | Actions used in CI workflows |
| `docker` | `/api` | Base images |
| `docker` | `/worker` | Base images |
| `docker` | `/relay` | Base images |

Grouping and PR rules:

- **Production deps** are grouped into one PR per ecosystem (minor + patch).
- **Dev deps** are grouped into one PR per ecosystem (minor + patch).
- **Major version bumps** are excluded from groups and opened as standalone PRs
  so each major update is reviewed individually per Section 1, citing the
  upstream migration notes.
- Open PRs are capped at 5 per ecosystem.
- All Dependabot PRs carry the `dependencies` label and use the
  `chore(deps):` commit-message prefix.

The 7-day supply-chain cooldown (Section 3) is enforced at review time;
Dependabot has no native "exclude releases newer than N days" setting.
Security updates bypass the cooldown per Section 3.

**`pip` ecosystem and `uv.lock`:** Dependabot updates `pyproject.toml` but does
not regenerate `uv.lock`. The `dependabot-uv-lock` workflow
(`.github/workflows/dependabot-uv-lock.yml`) detects this and automatically runs
`uv lock`, committing the updated lockfile back to the PR branch so that
`uv sync --locked` in the `api` workflow passes without manual intervention.
