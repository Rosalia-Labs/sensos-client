# Migrations

Version-aware client migrations live under `migrations/versions`.

Each version directory represents the target repo version being entered during an
upgrade. Example:

```text
migrations/
  versions/
    0.2.0/
      00-some-change
      10-another-change
```

Rules:

- directory names must match the repo `VERSION` format
- migration steps must be executable files named like `00-step-name`
- `migrations/run FROM_VERSION TO_VERSION` runs each version directory where:
  - `version > FROM_VERSION`
  - `version <= TO_VERSION`
- migrations should be idempotent
- downgrades are not supported by the migration runner
