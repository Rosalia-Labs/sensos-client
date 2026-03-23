# Next Step: Migrate The Runtime Payload

The current rewrite has only rebuilt the machine-prep layer:

- `setup/00-preflight`
- `setup/01-packages`
- `setup/02-users`
- `setup/03-filesystem`
- `setup/04-python-venv`

That is necessary, but it is not yet enough to configure or run a SensOS device on a fresh Raspberry Pi OS install.

## Why this is the next step

In the old client, the first stage did more than install packages and create users. It also copied the full runtime payload into place and exposed it to the OS:

- copied the `files/` tree into `/sensos`
- linked config scripts and service scripts into `/usr/local/bin`
- linked unit files into `/etc/systemd/system`
- linked `/sensos/etc/nftables.conf` into `/etc/nftables.conf`

Reference: [SensOS/client/sensos/stage-base/00-sensos/00-run.sh](/Users/tkeitt/Projects/sensos-client/SensOS/client/sensos/stage-base/00-sensos/00-run.sh#L21)

The rewrite currently has no equivalent runtime asset tree and no setup step that installs it. That means a cloned repo can prepare a host, but it cannot yet provide the actual configuration commands or managed services that make the device useful.

## Recommended next deliverable

Create a new setup step whose only job is to install the runtime payload from this repo onto the target system.

Suggested shape:

1. Add a repo-owned runtime tree, for example:
   - `files/etc`
   - `files/lib`
   - `files/scripts`
   - `files/service_scripts`
   - `files/services`
2. Add `setup/05-runtime-assets`.
3. Make `setup/05-runtime-assets` do the following:
   - ensure the runtime tree exists in the repo
   - copy it into `${CLIENT_ROOT}` or the final install root
   - mark scripts executable
   - install or symlink command entrypoints into `/usr/local/bin`
   - install or symlink unit files into `/etc/systemd/system`
   - run `systemctl daemon-reload`
4. Keep service enabling separate from asset installation where possible, but allow a small default set of always-on units if they are clearly baseline.

## First assets to migrate

Migrate the minimum set that unlocks real device configuration first.

Priority 1:

- `config-network`
- `config-storage`
- `config-wifi`
- `config-modem`
- `config-hotspot`
- `config-time`
- `sensos-config`
- shared helpers in `files/lib`

Priority 2:

- `ensure-sensos-dir.service`
- `sensos-init.service`
- `run-sensos-init.sh`
- `etc/nftables.conf`
- `etc/sensos-ports.nft`
- `etc/chrony.conf`

Priority 3:

- sensor, audio, BirdNET, GPS, and monitoring services

## After `05-runtime-assets`

Once runtime assets exist in the new repo, the next setup scripts should be:

- `setup/06-system-config`
  - hardware enablement for I2C, 1-wire, SPI
  - persistent journald
  - baseline system symlinks such as chrony and nftables config
- `setup/07-services`
  - enable the baseline units that should survive reboot
  - leave optional feature services off until explicitly configured

References from the old client:

- hardware enablement: [SensOS/client/sensos/stage-base/00-sensos/05-run.sh](/Users/tkeitt/Projects/sensos-client/SensOS/client/sensos/stage-base/00-sensos/05-run.sh)
- baseline service enablement: [SensOS/client/sensos/stage-base/00-sensos/06-run.sh](/Users/tkeitt/Projects/sensos-client/SensOS/client/sensos/stage-base/00-sensos/06-run.sh)
- journald persistence: [SensOS/client/sensos/stage-base/00-sensos/08-run.sh](/Users/tkeitt/Projects/sensos-client/SensOS/client/sensos/stage-base/00-sensos/08-run.sh)

## Practical conclusion

The next step is not more package/user/bootstrap work. The next step is to recreate the old `/sensos/files` payload in the new repo and add a setup script that installs it in a controlled, idempotent way.
