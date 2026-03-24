# Versioning

This repo should use an explicit repo version as the install and migration key.

## Source Of Truth

The current desired client version lives in [`VERSION`](/Users/keittth/Projects/sensos-client/VERSION).

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

- `MAJOR`: breaking compatibility, incompatible state layout, or migration contract changes
- `MINOR`: backward-compatible features or additive setup/state changes
- `PATCH`: bug fixes and idempotence fixes that do not change the migration contract

## Installed State

Each machine should record local install state in:

[`etc/install-state.env`](/Users/keittth/Projects/sensos-client/etc/install-state.env)

Suggested fields:

```sh
INSTALLED_VERSION=0.1.0
INSTALLED_AT=2026-03-23T12:00:00Z
INSTALL_STATUS=complete
INSTALL_REVISION=abc1234
OS_ID=raspbian
OS_VERSION_ID=12
```

This file is machine-local state. It is not the repo's source of truth.

## Client Host Guard

Setup and upgrade must only run on approved client machines. The required host
marker lives outside the repo at:

[`/etc/sensos/host-role.env`](/etc/sensos/host-role.env)

Required content:

```sh
SENSOS_HOST_ROLE=client
```

Without that file, preflight, migrations, setup, and `./upgrade` all refuse to
run.

## What To Do

Before a release:

- decide whether the change is `MAJOR`, `MINOR`, or `PATCH`
- bump [`VERSION`](/Users/keittth/Projects/sensos-client/VERSION) intentionally
- if the release changes persisted state or setup behavior, add or update migration logic

When running setup on a Pi:

- `setup/00-preflight` reads the repo `VERSION`
- it compares that against [`etc/install-state.env`](/Users/keittth/Projects/sensos-client/etc/install-state.env)
- it determines whether this is a fresh install, reconfigure, repair, or upgrade
- later setup or migration steps should only mark the install complete after success

When updating with `git fetch` or `git pull`:

- cache the pre-pull repo version and revision before updating the checkout
- re-run setup
- compare installed version to repo version
- run any needed migrations in order
- update install-state only after the update succeeds

The repo includes a top-level [`upgrade`](/Users/tkeitt/Projects/sensos-client/upgrade)
script for this flow. It:

- requires a clean git worktree before pulling
- uses `git pull --ff-only`
- runs version-aware migrations from [`migrations/versions`](/Users/tkeitt/Projects/sensos-client/migrations/versions)
- reruns setup
- records install-state only after success
- rolls the repo back to the previous git revision and reapplies setup if the
  post-pull upgrade fails

## Reminder

- do not use git SHA as the migration key
- do use git SHA as trace metadata
- keep migrations idempotent
- record installed version only after successful completion
