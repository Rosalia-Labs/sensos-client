# Versioning

This repo should use an explicit repo version as the install and migration key.

## Source Of Truth

The current desired client version lives in [`VERSION`](../VERSION).

Format:

```text
MAJOR.MINOR.PATCH
MAJOR.MINOR.PATCH-suffix
```

Examples:

```text
0.1.0
0.1.0-dev
0.2.0-alpha.1
1.0.0
```

Git commit hashes are useful for traceability, but they are not the primary version.

## Meaning

Before `1.0.0`, this repo should be treated as pre-stable:

- `MAJOR`: reserved for the eventual stable compatibility line (`1.0.0` and beyond)
- `MINOR`: the main release boundary during `0.x`; may include breaking deployment, state-layout, or migration-contract changes
- `PATCH`: bug fixes and idempotence fixes that do not intentionally change the desired setup contract

After `1.0.0`, use stricter semver-style meaning:

- `MAJOR`: breaking compatibility, incompatible state layout, or migration contract changes
- `MINOR`: backward-compatible features or additive setup/state changes
- `PATCH`: bug fixes and idempotence fixes that do not change the migration contract

## Installed State

Each machine should record local install state in the deployed overlay:

[`/sensos/etc/install-state.env`](/sensos/etc/install-state.env)

Suggested fields:

```sh
INSTALLED_VERSION=0.1.0
INSTALLED_AT=2026-03-23T12:00:00Z
INSTALL_STATUS=complete
INSTALL_REVISION=abc1234
OS_ID=raspbian
OS_VERSION_ID=12
```

This file is machine-local state inside the deployed `/sensos` tree. It is not
the repo's source of truth.

## What To Do

Before a release:

- decide whether the change is `MAJOR`, `MINOR`, or `PATCH`
- bump [`VERSION`](../VERSION) intentionally
- if the release changes persisted state or setup behavior, add or update migration logic

During `0.x`, prefer `MINOR` bumps for meaningful deployment-model or setup-contract changes, even when they are not backward-compatible.

## Working Mode During Active Stabilization

While the repo is still in a high-churn bug-swatting phase, use a rolling
`-dev` version for the current migration boundary.

Example:

```text
0.4.0-dev
```

In this mode:

- do not bump `PATCH` for every bug fix or idempotence fix
- keep the same `MAJOR.MINOR.PATCH-dev` while you are stabilizing one intended deployment contract
- bump `MINOR` when the migration boundary changes in a meaningful way
  - service renames
  - command renames
  - state-layout changes
  - install or upgrade contract changes
- cut the non-`-dev` version only when that migration line is stable enough to treat as a release boundary

This keeps the migration key meaningful without forcing noisy version churn for every repair commit.

When running setup on a Pi:

- `setup/00-preflight` reads the repo `VERSION`
- it compares that against [`/sensos/etc/install-state.env`](/sensos/etc/install-state.env)
- it determines whether this is a fresh install, reconfigure, repair, or upgrade
- later setup or migration steps should only mark the install complete after success

When updating with `git fetch` or `git pull`:

- cache the pre-pull repo version and revision before updating the checkout
- re-run setup
- compare installed version to repo version
- run any needed migrations in order
- update install-state only after the update succeeds

The repo includes a top-level [`upgrade`](../upgrade)
script for this flow. It:

- requires a clean git worktree before pulling
- uses `git pull --ff-only`
- runs version-aware migrations from [`migrations/versions`](../migrations/versions)
- redeploys `overlay/` into `/sensos` and reruns setup
- records install-state only after success

The repo also includes a top-level [`install`](../install)
script for first-time deployment. It prompts with a `[y/N]` warning, then runs
the repo's setup scripts and deploys the live overlay into the host plus `/sensos`.

## Reminder

- do not use git SHA as the migration key
- do use git SHA as trace metadata
- keep migrations idempotent
- record installed version only after successful completion
