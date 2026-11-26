# Failure patterns

Each pattern gives the signature to look for, the cause, and the fix. A fix
that changes files is a companion commit (`SKILL.md`, Companion commits).

## The checks ran on an old base

- **Signature:** a check fails, or the merge is blocked as "not up to date",
  after other pull requests merged.
- **Fix:** `gh api -X PUT repos/{owner}/{repo}/pulls/<N>/update-branch`, then
  wait for the new run.

## `pip`: `uv sync --locked` fails on Dependabot's commit

- **Cause:** Dependabot changes `api/pyproject.toml` and not `api/uv.lock`.
  The `dependabot-uv-lock` workflow rebuilds the lock and pushes it. Its check
  is named `regenerate`.
- **Fix:** wait for `regenerate`. If it passed and `check` still shows the old
  failure, update the branch.

## `pip`: `regenerate` fails with `No solution found`

- **Cause:** the bump needs another dependency to move, and this repository
  pins that one. In #4132, `grpcio-tools` 1.85 needed `protobuf` 7 while
  `api/pyproject.toml` holds `protobuf<7`.
- **Fix:** none here. Moving the other pin is a decision for the owner. Leave
  the pull request open and quote the resolver's `cause:` lines in your
  comment.

## No CI run starts after the lock was pushed

- **Cause:** the workflow pushes with a GitHub App token. The App must be
  installed, and the secrets `CLIENT_ID` and `APP_PRIVATE_KEY` must exist both
  as Actions secrets and as Dependabot secrets
  (`docs/dev/DEPENDENCIES.md` Section 6).
- **Fix:** none here. Report it under Attention.

## `supply-chain-cooldown`: the trailer disagrees with the adopted version

- **Signature:** the status description reads
  `trailer <old> disagrees with adopted versions ['<new>']`.
- **Cause:** Dependabot moved the branch to a newer release, and its commit
  message still names the older one.
- **Fix:** comment `@dependabot recreate` on the pull request. Do this before
  any companion commit, because it discards them.

## `docker` `/api`: a uv bump fails `check_uv_pins.py`

- **Signature** (`check`): `api/Dockerfile: expected uv:<old>, found ['<new>']`.
- **Cause:** Dependabot bumps `astral-sh/uv` in `api/Dockerfile` only. Every
  other uv pin must name the same version.
- **Fix** (as in #3530 and #3630): replace the old version in every file that
  `git grep -l -F '<old>' -- . ':!api/uv.lock'` lists. Check with
  `python3 scripts/check_uv_pins.py`.
- **The hooks now need the new uv.** `api/pyproject.toml` requires the exact
  version, and every other worktree on this host still requires the old one.
  Do not update the host's uv before the merge. Install the new one beside it
  and put it first on `PATH` for your commit and your push:

  ```sh
  uv venv <scratch>/uv-new
  uv pip install --python <scratch>/uv-new 'uv==<new>'
  PATH="<scratch>/uv-new/bin:$PATH" git push origin HEAD
  ```

## `docker-compose`: an image bump fails `check_image_pins.py`

- **Signature** (`check`): `check-image-pins: the <image> CI pins do not
  follow compose.yaml`.
- **Cause:** CI pins PostgreSQL and SeaweedFS by digest in workflow files that
  Dependabot does not read.
- **Fix** (as in #3528 and #3628): follow the procedure for that image in
  `docs/dev/DEPENDENCIES.md` Section 6. It gives the digest to use and the
  date to check against the cooldown. Check with
  `python3 scripts/check_image_pins.py`.

## `docker-compose`: an end-to-end job fails

- **Cause:** a service image bump can break the end-to-end tests, most often
  a major version.
- **Fix:** read the log. If the bump causes the failure, leave the pull
  request open. If the failure is unrelated, prove it with the three steps in
  `SKILL.md`.

## The protocol stubs drift after `grpcio-tools` or `protobuf` moves

- **Signature:** the `proto` workflow or `make proto-check` fails, because the
  checked-in Python stubs no longer match the generator.
- **Fix** (as in #3468): run `make proto-gen` on the branch and commit the
  regenerated stubs, with any change the new API needs.
- **On the lock refresh:** the workflow force-pushes its branch on every run,
  each Monday at 04:00 UTC and on manual dispatch. That discards companion
  commits. Merge before the next run, or make the commit again after it.

## After the run: re-sync the root checkout

Once the root checkout has pulled the merges, update the local tools that
they moved:

- A uv bump: `uv self update <version>`. A worktree that has not merged
  `main` yet still needs the old version.
- An `npm` bump: `cd webui && npm ci`. Stale `node_modules` breaks the webui
  type check in `make check`.
