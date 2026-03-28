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
7. other feature-specific commands as needed

Notes:

- `config-storage` can be run earlier. In practice it is often run after network and location are set, but before recording runs for long.
- Commands that change system state usually expect `sensos-admin` or use `sudo` internally.

## Top-Level Repo Commands

### `./install`

Bootstraps a fresh SensOS client install onto the current machine.

Typical use:

```sh
./install
```

Behavior:

- must be run as the bootstrap user, not `root`
- confirms the target deploy root
- runs the full setup pipeline with eager APT and Python dependency install

### `./upgrade`

Pulls the latest repo changes, runs migrations, and reapplies setup to the deployed client.

Typical use:

```sh
./upgrade
./upgrade --refresh
./upgrade --refresh-apt
./upgrade --refresh-pip
```

Behavior:

- must be run from a clean git worktree
- requires the current branch to have an upstream
- runs migrations between installed and repo versions
- reruns setup after pull

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
- warns if time sync or location is missing

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
config-wifi --ssid MySSID --password 'secretpass' --start true
```

Behavior:

- sets autoconnect based on `CONNECTIVITY_MODE` from `network.conf`
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
- sets autoconnect from `CONNECTIVITY_MODE`
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
- `--time-drift-sec`
- `--enable-service`
- `--start-service`

Typical use:

```sh
config-gps --start-service true
config-gps --interval 60 --location-drift-m 50 --time-drift-sec 30
```

Behavior:

- writes `/sensos/etc/gps.conf`
- can update time and location automatically from GPS
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

Typical use:

```sh
config-birdnet --download-models --start-service
config-birdnet --backend litert --start-service
config-birdnet --disable
```

Behavior:

- writes `/sensos/etc/birdnet.env`
- can download BirdNET models before enabling
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

### `debug-network-capture`

Runs a temporary bounded packet-capture session for debugging.

Subcommands:

- `start`
- `status`
- `stop`
- `report`
- `cleanup`

Typical use:

```sh
debug-network-capture start --hours 24
debug-network-capture status
debug-network-capture report --latest --cleanup
```

Behavior:

- stores temporary capture sessions under `/sensos/log/network_capture/sessions`
- uses bounded rotating `pcap` files
- is intended for debugging, not permanent collection

### `report-network-capture`

Generates a report from a capture session.

Typical use:

```sh
report-network-capture --capture-root /sensos/log/network_capture/sessions/<timestamp> --hours 0 --top 20
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
```

Then add optional features as needed:

```sh
config-wifi --ssid <ssid> --password <pass> --start true
config-modem --service 1nce --start true
config-gps --start-service true
config-birdnet --download-models --start-service
```
