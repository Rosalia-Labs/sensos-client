# Getting Started (Tutorial)

This guide is the shortest path to bring up a SensOS client device.
For full command syntax, flags, and edge cases, use the
[`Command Reference`](command-reference.md).

## Typical Setup Sequence

1. Clone repo
2. `./install`
3. `config-time`
4. `config-network`
5. `config-location`
6. `config-storage`
7. `config-arecord`
8. `config-i2c-sensors`
9. `config-i2c-uploads`
10. `config-birdnet`
11. other feature-specific setup as needed

Notes:

- `config-storage` can be run earlier, but run it before long recording sessions.
- Commands that change system state usually expect `sensos-admin` or use `sudo` internally.

## Before You Start

- Use a Debian-family host/device image supported by this repo.
- Confirm you have server setup endpoint details from your SensOS server operator.
- Have a target network name ready (for example `testing` or `biosense`).

## 1. Clone Repo

Clone the client repo onto the device or staging machine:

```sh
git clone https://github.com/Rosalia-Labs/sensos-client.git
cd sensos-client
```

## 2. Install Client Runtime

Run from the repo root on the client device:

```sh
./install
```

If this is a retry after a failed install, use:

```sh
./install --reinstall
```

Reference: [`./install`](command-reference.md#top-level-repo-commands)

## 3. Set Clock And Connectivity

```sh
config-time
config-network --setup-server <server-host-or-ip> --network <network-name>
```

Run `config-time` first, then enroll with `config-network`.

Reference:

- [`config-time`](command-reference.md#config-time)
- [`config-network`](command-reference.md#config-network)

## 4. Set Device Metadata And Storage

```sh
config-location --latitude <lat> --longitude <lon>
config-storage
```

Reference:

- [`config-location`](command-reference.md#config-location)
- [`config-storage`](command-reference.md#config-storage)

## 5. Configure Optional Features

Enable only what this deployment needs:

- Audio capture and storage workflow
- I2C sensors / uploads
- BirdNET

Reference:

- [BirdNET setup](birdnet.md)
- [I2C upload API](i2c-uploads.md)
- [Network capture](network-capture.md)
- Full command list in [`Command Reference`](command-reference.md)

## 6. Validate And Move To Field

For staged deployment and network cutover, follow:

- [`Staged Provisioning And Network Cutover`](command-reference.md#staged-provisioning-and-network-cutover)
- [`prep-for-deployment`](command-reference.md#prep-for-deployment)
- [`field-deploy`](command-reference.md#field-deploy)

## Optional: Upload Hardware Inventory

Upload hardware inventory after enrollment, ideally on the final deployed
hardware:

```sh
upload-hardware-profile
```

Reference: [`upload-hardware-profile`](command-reference.md#upload-hardware-profile)

## Ongoing Operations

- Upgrade an installed client: `./upgrade`
- See upgrade behavior and offline mode: [`./upgrade`](command-reference.md#upgrade)

## Related Docs

- [QEMU testing](qemu-testing.md)
- [Security checklist](security-checklist.md)
- [Developer security notes](security-development.md)
