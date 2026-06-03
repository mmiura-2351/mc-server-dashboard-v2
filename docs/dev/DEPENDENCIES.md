# Dependency Policy

How v2 takes on and updates third-party dependencies. This document fixes the
*policy*; the concrete mechanisms (lockfile commands, automated-updater
configuration, the cooldown enforcement per tool) are set up as the toolchain
lands and are marked *(forthcoming)*.

> **Two ecosystems.** `api/` is Python and `worker/` is Go. The policy below
> applies to both; where a rule is expressed per-tool, each ecosystem's section
> states its own form.

## 1. Pinning style

- **Direct runtime dependencies** are constrained so compatible updates flow in
  but a **major (breaking) bump is always explicit and PR-reviewed**.
  - Python (`api/`): a version range that admits minor/patch and caps the next
    major (for a `0.x` library, cap the next minor, since `0.x` may break on a
    minor bump).
  - Go (`worker/`): a major version is part of the module import path, so a
    major bump is inherently a separate, explicit change; minor/patch updates
    are selected by the module graph.
- **Dev / tooling dependencies** (linters, test runners, type checkers, build
  helpers) are kept current with no upper bound — they do not affect runtime
  behavior and staying current is cheap.
- **Transitive dependencies** are not declared by hand; they are pinned in the
  lockfile, which is the single source of truth.

## 2. Lockfiles

- Each ecosystem commits its lockfile (`api/`: the uv lockfile; `worker/`:
  `go.sum` with `go.mod`). Lockfiles are the reproducibility boundary and must
  be committed.
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

An automated dependency updater runs on a schedule per ecosystem, with:

- Updates **grouped** (production vs. dev) to keep PR volume low.
- A capped number of open PRs.
- The cooldown from Section 3 applied; major bumps held longer and opened as
  **standalone** PRs (a major update is reviewed on its own, never bundled),
  citing the upstream migration notes.

The concrete updater configuration (schedule, grouping, labels, cooldown days)
lives in repository configuration *(forthcoming)*.
