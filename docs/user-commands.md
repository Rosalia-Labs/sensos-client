# User Commands

This page documents the user-facing commands in this repo: the normal bring-up sequence, what each command does, and the commands you are likely to run during setup, reconfiguration, or debugging.

Most of these commands are installed onto the device under `/usr/local/bin` by the SensOS client install.

## Typical Setup Sequence

Typical order on a newly configured device:

1. `config-time`
2. `config-network`
3. `config-location`
4. `config-storage`
5. `config-arecord`
6. `config-i2c-sensors`
7. `config-birdnet`
8. other feature-specific commands as needed

Notes:

- `config-storage` can be run earlier. In practice it is often run after network and location are set, but before recording runs for long.
- Commands that change system state usually expect `sensos-admin` or use `sudo` internally.

## SSH Over Cellular

When SSHing to cellular-connected devices, disable OpenSSH keystroke-obscuring traffic on the client you are typing from.

Use:

```sh
ssh -o ObscureKeystrokeTiming=no <host>
```

or add this to your local `~/.ssh/config`:

```sshconfig
Host *
    ObscureKeystrokeTiming no
```

This is recommended for IoT cellular devices because OpenSSH may otherwise send extra fake keystroke packets, which can create unnecessary background traffic on metered links.

## Top-Level Repo Commands

### `./install`

Bootstraps a fresh SensOS client install onto the current machine.

Typical use:

```sh
./install
./install --restart
./install --install-recommends
```

Behavior:

- must be run as the bootstrap user, not `root`
- confirms the target deploy root
- runs the full setup pipeline with eager APT and Python dependency install
- `--restart` clears transient install artifacts such as managed venvs and install-state before rerunning setup; use it after a failed install if you pulled newer repo contents and want to avoid reusing partial Python/setup state
- `--install-recommends` allows APT to install recommended packages during setup; the default install path still uses `--no-install-recommends`

### `./upgrade`

Pulls the latest repo changes, runs migrations, and reapplies setup to the deployed client.

Typical use:

```sh
./upgrade
./upgrade --offline
./upgrade --force-package-updates
```

Behavior:

- in normal mode, it must be run from a clean git worktree
- in normal mode, the current branch must have an upstream
- only use it for a machine that already has a successful SensOS install
- if `./install` failed and you just need newer repo contents before retrying, run `git pull` manually and then rerun `./install`; do not use `./upgrade` as install recovery
- runs migrations between installed and repo versions
- reruns setup after pull
- `--force-package-updates` forces both APT metadata/package reconciliation and Python dependency reinstall
- `--offline` skips `git pull` and upgrades from the repo contents already on disk; this is useful when files were copied from a laptop to a client without internet access
- even in offline mode, the run can still fail if package or Python dependency changes require network access

## Core Bring-Up Commands

### `config-time`

Interactive time and timezone setup.

Use it to:

- inspect current system time and sync status
- check whether `chrony` is configured and active
- set timezone
- set the clock manually if needed

Typical use:

```sh
config-time
```

Run this first. Accurate time matters before recording or storing sensor data.

### `config-network`

Primary network enrollment command. This is the core command that registers the device, provisions WireGuard, writes `/sensos/etc/network.conf`, configures SSH policy, and sets up device identity.

Important flags from the source:

- `--config-server`
- `--port`
- `--network`
- `--subnet`
- `--wg-endpoint`
- `--wg-keepalive`
- `--disable-ssh-passwords`
- `--note`
- `--force`

Typical use:

```sh
config-network --config-server <server-ip-or-name> --network <network-name>
config-network --config-server <server-ip-or-name> --network sensos --subnet 1
```

Main gotchas:

- `--config-server` is only the server address reachable from the current setup environment.
- the server will usually return a `wg_endpoint` suitable for the chosen network, but if the deployed device must reach a different public or routed endpoint, you need to override it with `--wg-endpoint`
- `--network` is now required and must be supplied explicitly for every enrollment
- `--subnet` is normally not required for device enrollment
- in the current server implementation, the server searches for the first available host IP starting at the requested subnet offset; with the normal default of `1`, allocation starts at `x.x.1.1`
- subnet `0` is reserved for admin containers and computers, so normal device enrollments should start at subnet `1` or later

Run this before commands that need:

- `CLIENT_WG_IP`
- `SERVER_WG_IP`
- API password

### `config-location`

Writes the local location config and, when network credentials are present, pushes the same location to the server.

Important flags:

- `--latitude`
- `--longitude`
- `--config-server`
- `--port`

Typical use:

```sh
config-location --latitude 30.2672 --longitude -97.7431
```

Behavior:

- always writes `/sensos/etc/location.conf`
- syncs location to the server when `network.conf`, `CLIENT_WG_IP`, and API password are available

### `config-storage`

Prepares the data layout under `/sensos`, and optionally formats/mounts a separate data disk.

Important flags:

- `--device`
- `--no-fstab`

Typical use:

```sh
config-storage
config-storage --device /dev/sda
config-storage --device /dev/sda --no-fstab
```

Behavior:

- if no device is supplied, prompts interactively
- can prepare `/sensos/data` on the current filesystem without a separate disk
- can partition, format, mount, and persist a selected block device
- this is the provisioning step for data storage; it is not a replacement for `archive-mode`
- use `config-storage` when you are setting up or changing where `/sensos/data` lives
- use `archive-mode` when storage is already configured and you need a safe temporary archive window to copy data off, swap media, or clear `/sensos/data`

### `archive-mode`

Enters or exits a temporary archival state for `/sensos/data`.

Typical use:

```sh
archive-mode --status
archive-mode --enter
archive-mode --exit
archive-mode --exit --clear-data
```

Behavior:

- `--status` reports whether archive mode is active, whether `/sensos/data` is mounted, and whether the main data writers are active
- `--enter` stops the main `/sensos/data` writers, checkpoints SQLite databases, and syncs storage
- entering archive mode writes a state marker so exit/clear operations are tied to a real prepared archive window
- `--exit` remounts `/sensos/data` if needed and restarts the stopped services
- `--exit --clear-data` clears `/sensos/data` in place before restarting services, which is useful after copying an entire epoch off-device
- use it for both copy-off and media-swap workflows
- after `--enter`, either copy data off the device or swap media, then use `--exit`
- `archive-mode --exit --clear-data` is the normal non-interactive way to clear an already configured `/sensos/data` after an archive window

### `summarize-data-dir`

Prints a bounded summary of a data tree without dumping a full recursive listing over SSH.

Typical use:

```sh
summarize-data-dir
summarize-data-dir --top 20
summarize-data-dir --path /sensos/data/audio
```

Behavior:

- defaults to `/sensos/data`
- reports total directory size, file and directory counts, and filesystem usage
- shows the largest top-level entries by size
- shows the largest files and most recently modified files
- keeps output compact even when the tree is very large, though it still scans the tree locally on the device

### `config-arecord`

Configures raw audio recording and optionally enables/starts the recording service.

Important flags:

- `--device`
- `--use-plughw`
- `--channels`
- `--max-time`
- `--format`
- `--rate`
- `--base-dir`
- `--enable-service`
- `--start-service`

Typical use:

```sh
config-arecord
config-arecord --device plughw:1,0 --channels 2 --rate 48000 --start-service true
```

Behavior:

- warns if time sync or location is missing
- may ask to stop active recording/compression/thinning services before reconfiguring
- writes recording config and can enable/start `sensos-arecord.service`

### `config-i2c-sensors`

Configures periodic I2C sensor polling and optionally enables/starts the reader service.

Important flags:

- `--interval`
- `--bme280-0x76-interval`
- `--bme280-0x77-interval`
- `--scd30-interval`
- `--scd4x-interval`
- `--ads1015-interval`
- `--enable-service`
- `--start-service`

Typical use:

```sh
config-i2c-sensors
config-i2c-sensors --interval 60 --scd30-interval 120 --start-service true
```

Behavior:

- writes `/sensos/etc/i2c-sensors.conf`
- ensures `/sensos/data/microenv` exists with shared permissions
- installs optional I2C/GPIO Python dependencies on demand before enabling the reader service
- warns if time sync or location is missing

### `config-rpi-eeprom`

Reads and updates Raspberry Pi bootloader EEPROM settings related to board power policy.

Important flags:

- `--show`
- `--set-psu-max-current`
- `--unset-psu-max-current`

Typical use:

```sh
config-rpi-eeprom --show
config-rpi-eeprom --set-psu-max-current 5000
config-rpi-eeprom --unset-psu-max-current
```

Behavior:

- preserves unrelated EEPROM settings
- sets or removes `PSU_MAX_CURRENT`
- requires `rpi-eeprom-config`
- prints a reboot-required message after applying changes

### `config-hardware-profile`

Lists, shows, and applies named hardware profiles shipped with the client.

Important flags:

- `--list`
- `--show`
- `--status`
- `--apply`
- `--unapply`

Typical use:

```sh
config-hardware-profile --list
config-hardware-profile --show geekworm-ups
config-hardware-profile --status
config-hardware-profile --apply geekworm-ups
config-hardware-profile --unapply geekworm-ups
```

Behavior:

- reads profile TOML files from `/sensos/profiles`
- tracks applied profile names in `/sensos/etc/hardware-profile-state.json`
- applies only the settings supported by the current client version
- can reverse only the settings supported by the current client version
- currently supports EEPROM `PSU_MAX_CURRENT` through `config-rpi-eeprom`
- keeps the profile model intentionally simple; there is no profile inheritance yet

## Connectivity-Specific Commands

These are usually run after `config-network`.

### `config-wifi`

Creates or updates a Wi‑Fi client connection using NetworkManager.

Important flags:

- `--ssid`
- `--password`
- `--iface`
- `--start`
- `--hidden`
- `--limit-up-kbit`
- `--limit-down-kbit`

Typical use:

```sh
config-wifi --ssid MySSID --password 'secretpass' --iface wlan1 --start true
```

Behavior:

- usually used on `wlan1` when the device also exposes an AP on `wlan0`
- Wi-Fi client mode and AP mode are mutually exclusive on the same interface
- do not run `config-wifi` and `config-hotspot` against the same NIC unless you intend one to replace the other
- if the device has only one Wi-Fi NIC and that NIC must join an upstream Wi-Fi network, the device cannot also host a local AP at the same time
- optionally applies traffic caps with `tc`
- registers the interface with `vnstat` when available

### `config-modem`

Creates or updates a cellular modem connection using NetworkManager.

Important flags:

- `--service`
- `--device`
- `--start`
- `--limit-up-kbit`
- `--limit-down-kbit`

Typical use:

```sh
config-modem --service 1nce --start true
config-modem --service soracom --device cdc-wdm0
```

Behavior:

- currently supports `1nce` and `soracom`
- can apply traffic caps
- registers the modem interface with `vnstat`
- applies a WireGuard MTU adjustment for 1NCE

### `config-hotspot`

Configures the device as a Wi‑Fi access point.

Important flags:

- `--ssid`
- `--password`
- `--power-save`
- `--interface`
- `--channel`

Typical use:

```sh
config-hotspot --password 'fieldsetup123'
config-hotspot --ssid sensos-setup --password 'fieldsetup123' --interface wlan0
```

Behavior:

- requires `NETWORK_NAME` and `CLIENT_WG_IP` from `network.conf`
- derives a default SSID from the network name and WG IP when `--ssid` is not supplied
- usually used on `wlan0` when Wi‑Fi client mode is handled separately on `wlan1`
- AP mode and Wi‑Fi client mode are mutually exclusive on the same interface
- if the device has only one Wi-Fi NIC and that NIC is needed for `config-wifi`, you cannot keep the AP active on that same NIC
- configures AP mode with NetworkManager

## Optional Feature Commands

### `config-gps`

Configures the optional GPS integration service.

Important flags:

- `--disable`
- `--backend`
- `--i2c-addr`
- `--i2c-bus`
- `--interval`
- `--sync-time`
- `--update-location`
- `--location-drift-m`
- `--time-conflict-sec`
- `--enable-service`
- `--start-service`

Typical use:

```sh
config-gps --start-service true
config-gps --interval 60 --location-drift-m 50
config-gps --time-conflict-sec 300
```

Behavior:

- writes `/sensos/etc/gps.conf`
- installs optional GPS Python dependencies on demand before enabling the GPS service
- can update time and location automatically from GPS
- when NTP does not appear healthy, a valid GPS fix becomes the active time source
- reports a GPS/NTP time conflict instead of overriding a synchronized clock when the difference is too large
- controls `sensos-gps.service`

### `config-birdnet`

Configures optional host-native BirdNET processing.

Important flags:

- `--disable`
- `--enable-service`
- `--start-service`
- `--download-models`
- `--models-url`
- `--backend`
- `--input-mode`

Typical use:

```sh
config-birdnet --download-models --start-service
config-birdnet --backend litert --start-service
config-birdnet --input-mode split-channels --start-service
config-birdnet --disable
```

Behavior:

- writes `/sensos/etc/birdnet.env`
- can download BirdNET models before enabling
- supports `mono` and `split-channels` multichannel input handling
- controls `sensos-birdnet.service`

### `install-birdnet-models`

Downloads and installs the BirdNET model bundle under `/sensos/birdnet`.

Important flags:

- `--url`
- `--force`

Typical use:

```sh
install-birdnet-models
install-birdnet-models --force
```

## Debug and Reporting Commands

### `debug-gps`

Shows GPS service status and the latest state the GPS worker reported.

Typical use:

```sh
debug-gps
```

Behavior:

- reports whether `sensos-gps.service` is enabled and active
- shows whether the latest worker state has a current fix
- shows the latest state message and timestamps
- dumps `/sensos/etc/gps.conf`, `/sensos/data/microenv/gps-state.env`, and recent GPS logs

### `packet-tracing`

Runs a temporary bounded packet-capture session for debugging.

This command was previously documented as `package-tracing`. The installed command name is `packet-tracing`.

Subcommands:

- `start`
- `status`
- `stop`
- `report`
- `cleanup`

Typical use:

```sh
packet-tracing start --duration 24
packet-tracing status
packet-tracing report --latest --cleanup
packet-tracing report --latest --save
packet-tracing cleanup --all
```

Behavior:

- stores temporary capture sessions under `/sensos/log/network_capture/sessions`
- uses bounded rotating `pcap` files
- prints reports to stdout by default
- only writes `report-*.txt` and `report-*.json` files when `--save` is used
- is intended for debugging, not permanent collection

### `debug-i2c`

Prints a compact hardware and runtime diagnostic report for I2C bring-up.

Typical use:

```sh
debug-i2c
```

Behavior:

- shows the resolved client root and I2C config path
- shows Raspberry Pi boot config and persistent module config for I2C, SPI, and 1-wire
- shows loaded modules, `/dev/i2c-*` nodes, and `sensos-runner` device access
- shows `sensos-read-i2c.service` status and recent logs
- runs `i2cdetect -y 1` when the I2C device node is present
- is intended for field debugging on deployed clients

### `report-network-capture`

Generates a report from a capture session.

Typical use:

```sh
report-network-capture --hours 0 --top 20
report-network-capture --capture-root /sensos/log/network_capture/sessions/<timestamp> --json
```

Behavior:

- summarizes traffic by direction, protocol, remote IP, local port, remote port, and full flow tuple
- useful when you need to identify persistent talkers or unexpected inbound sources

## Recommended Bring-Up Example

A common operator sequence looks like:

```sh
config-time
config-network --config-server <server>
config-location --latitude <lat> --longitude <lon>
config-storage
config-arecord --device plughw:1,0 --start-service true
config-i2c-sensors --start-service true
config-birdnet --start-service
```

Then add optional features as needed:

```sh
config-wifi --ssid <ssid> --password <pass> --start true
config-modem --service 1nce --start true
config-gps --start-service true
config-birdnet --download-models --start-service
```
