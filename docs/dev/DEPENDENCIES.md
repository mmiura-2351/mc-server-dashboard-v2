# Dependency Policy

How v2 takes on and updates third-party dependencies. This document fixes the
*policy* and names the concrete mechanisms that enforce it: lockfile commands
(Section 2), cooldown enforcement (Section 3), and the automated updater
(Section 6).

> **Three language ecosystems, plus images and actions.** `api/` is Python;
> `worker/` and `relay/` are Go; `webui/` is Node. Container base images
> (`docker`), the compose service images (`docker-compose`) and the CI actions
> (`github-actions`) are dependencies too, tracked by the same updater
> (Section 6). The policy below applies to all of them; where a rule is
> expressed per-tool, each ecosystem's section states its own form.

## 1. Pinning style

- **Direct runtime dependencies** are constrained so compatible updates flow in
  but a **major (breaking) bump is always explicit and PR-reviewed**.
  - Python (`api/`): a version range that admits minor/patch and caps the next
    major (for a `0.x` library, cap the next minor, since `0.x` may break on a
    minor bump).
  - Go (`worker/`, `relay/`): a major version is part of the module import
    path, so a major bump is inherently a separate, explicit change;
    minor/patch updates are selected by the module graph.
  - Node (`webui/`): caret ranges (`^x.y.z`), which admit minor/patch and cap
    the next major (for a `0.x` library, the next minor).
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

- Each ecosystem commits its lockfile (`api/`: `uv.lock`; `worker/` and
  `relay/`: `go.sum` with `go.mod`; `webui/`: `package-lock.json`). Lockfiles
  are the reproducibility boundary and must be committed. (`go.sum` is
  generated, and from then on committed, only once the module has external
  dependencies; a dependency-free module has only `go.mod`.)
- Reproducible installs resolve from the lockfile; routine updates regenerate it
  through the ecosystem's own command: `uv lock` (api), `go get` /
  `go mod tidy` (worker, relay), `npm install` / `npm update` (webui).

## 3. Supply-chain cooldown

To mitigate maintainer-account takeover, typosquatting, and compromised
releases — which can take several days to detect and retract — **do not adopt
any release published within the last 7 days**. Holding a 7-day window absorbs
most public-incident timelines.

- The cooldown applies to **every** ecosystem.
- It is enforced at the automated-update layer: `.github/dependabot.yml` sets
  `cooldown: {default-days: 7}` on every ecosystem, so Dependabot never opens a
  version-update PR for a release younger than 7 days. A resolver-side cooldown
  for `api/` (uv's `exclude-newer` option) is not configured; the cooldown is
  enforced by Dependabot's `cooldown` setting and by the merge-time
  `supply-chain-cooldown` status (next bullet). Whether to add a resolver-side
  cooldown is undecided.
- It is also enforced at **merge time** as a backstop to the opening-side
  `cooldown` setting. The `supply-chain-cooldown` workflow
  (`.github/workflows/supply-chain-cooldown.yml`, driving
  `scripts/supply_chain_cooldown.py`) reads each Dependabot PR's bumped packages
  from its commit trailer, looks up each release's publish date in the upstream
  registry (PyPI, npm, `proxy.golang.org`, GitHub Releases for actions and
  `ghcr.io` images, Docker Hub for Docker Hub images), and posts a
  `supply-chain-cooldown` commit status: `failure` (plus a `supply-chain-cooldown`
  label) while any release is younger than 7 days, `success` once it ages out.
  The trailer names a container image without its registry host, so the gate
  recovers the host from that image's pinned `FROM` / `image:` reference in the
  checked-out tree — which is why the workflow's checkout step is load-bearing. A
  daily schedule re-runs it so blocked PRs unblock themselves with no new commit.
  `supply-chain-cooldown` is a required status check on `main` alongside `check`,
  which is what makes it enforceable rather than advisory.
- Because it is required, the workflow runs for **every** PR, not only
  Dependabot's: a required commit status is satisfied only by being posted, and a
  skipped job posts nothing, so a PR whose status never arrives can never merge.
  A non-Dependabot PR adopts no Dependabot-managed pin, so it gets an immediate
  `success` pass-through ("Not a Dependabot PR").
- **Security updates bypass the cooldown** (see Section 4); a known-exploited
  vulnerability outweighs the supply-chain risk window. The merge-time gate
  detects them from the advisory reference (GHSA id) Dependabot emits and lets
  them pass immediately; this relies on Dependabot alerts being enabled, without
  which Dependabot opens no security-update PRs.

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
| `docker-compose` | `/` | Service images in `compose.yaml` |

Grouping and PR rules:

- **Production deps** are grouped into one PR per ecosystem (minor + patch).
- **Dev deps** are grouped into one PR per ecosystem (minor + patch).
- **Major version bumps** are excluded from groups and opened as standalone PRs
  so each major update is reviewed individually per Section 1, citing the
  upstream migration notes.
- Open PRs are capped at 5 per ecosystem.
- All Dependabot PRs carry the `dependencies` label and use the
  `chore(deps):` commit-message prefix.

The 7-day supply-chain cooldown (Section 3) is enforced automatically: every
ecosystem entry sets `cooldown: {default-days: 7}`, so Dependabot does not open
a version-update PR for any release younger than 7 days. Cooldown applies to
version updates only; security updates bypass it per Section 3.

**`pip` ecosystem and `uv.lock`:** Dependabot updates `pyproject.toml` but does
not regenerate `uv.lock`. The `dependabot-uv-lock` workflow
(`.github/workflows/dependabot-uv-lock.yml`) detects this and automatically runs
`uv lock`, committing the updated lockfile back to the PR branch so that
`uv sync --locked` in the `api` workflow passes. The workflow pushes using a
GitHub App token (via `actions/create-github-app-token`) so the resulting commit
triggers CI; this requires the repository secrets `CLIENT_ID` and `APP_PRIVATE_KEY`
to be configured for the App. Because Dependabot-triggered workflows cannot
access regular repository secrets, these two secrets must also be added to the
**Dependabot secrets** (Settings > Secrets and variables > Dependabot).

**Pins no ecosystem watches:** the `docker` ecosystems read `Dockerfile`s and
`docker-compose` reads `compose.yaml`, so an image pinned anywhere else is
invisible to Dependabot and is bumped by hand:

- **PostgreSQL** — `services.postgres.image` in `.github/workflows/api.yml`,
  `e2e.yml` and `webui-e2e.yml`, plus `PG_IMAGE` in
  `scripts/run_webui_e2e.sh`. Follows the `db` image in `compose.yaml`.
- **SeaweedFS** — the `docker run` line in `api.yml`'s `live-s3` job. Follows
  the `seaweedfs` image in `compose.yaml` (it currently lags: `4.41` in CI
  against `4.42` deployed — issue #2904).

**PostgreSQL: CI runs the minor the deployment runs** (#2755). `compose.yaml`
pins an explicit minor rather than the floating `postgres:18`, so a new minor
arrives as a Dependabot `docker-compose` PR instead of silently on the next
`docker compose pull`. That PR is where the four CI references above are
re-pinned, by hand, to the same minor's Debian digest — CI and the deployment
move together, in one reviewed change. Read the digest for the new minor from
the registry (`docker buildx imagetools inspect postgres:<minor>`) and the date
from Docker Hub's `tag_last_pushed` for that tag — the field
`scripts/supply_chain_cooldown.py` reads, not the
`org.opencontainers.image.created` annotation `imagetools` prints, which is the
upstream build time and predates the push. Keep the
`# postgres:<minor> (Debian; pushed <date>, outside the cooldown)` comment
truthful: the 7-day cooldown (Section 3) applies to these hand-maintained pins
exactly as it does to the automated ones.
