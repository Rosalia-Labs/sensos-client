# QEMU Testing

This directory contains the local helper for running a Debian Trixie ARM64 VM on Apple Silicon with MacPorts QEMU:

- [run-debian-trixie-arm64](/Users/tkeitt/Projects/sensos-client/test/qemu/run-debian-trixie-arm64)

## Artifacts

VM artifacts live under:

`test/qemu/artifacts/`

That path is gitignored.

Layout:

- `test/qemu/artifacts/images/debian-trixie-arm64-base.qcow2`
- `test/qemu/artifacts/images/debian-trixie-arm64-data.qcow2`
- `test/qemu/artifacts/images/edk2-arm64-vars.fd`
- `test/qemu/artifacts/iso/debian-trixie-arm64-netinst.iso`

By default, the helper creates two 32 GB qcow2 disks:

- base/system disk: `32G`
- data disk: `32G`

## Workflow

1. Put a Debian ARM64 installer ISO at:

```bash
test/qemu/artifacts/iso/debian-trixie-arm64-netinst.iso
```

2. Create and install the base VM once:

```bash
test/qemu/run-debian-trixie-arm64 install
```

The `install` command recreates the base/system image first, so rerunning it
starts a fresh Debian install instead of reusing the previous VM state. It also
removes the data disk image and the writable UEFI vars file so the next boot
starts from a fully clean slate and stale boot
entries do not skip the installer ISO. The installer boot only attaches the
system disk so Debian cannot accidentally install itself onto the test data
disk.

3. Do any one-time guest bootstrap during the install boot or after its first
reboot. After that, use `update` when you want to keep guest changes without
doing a full reinstall:

```bash
test/qemu/run-debian-trixie-arm64 update
```

The `update` command boots the installed VM read/write, so changes to the base
system disk and attached data disk persist. Shut the guest down cleanly from
inside the VM before exiting QEMU or writes may be lost.

4. Use disposable `run` boots when you want a non-sticky test session:

```bash
test/qemu/run-debian-trixie-arm64 run
```

The `run` command uses `-snapshot`, so guest disk changes are discarded when
QEMU exits.

Before leaving the install phase, shut the guest down cleanly from inside the
VM. Files changed during install and bootstrap are only reliably persisted to
the base image after a clean shutdown.

## Guest bootstrap

The stock Debian guest does not include `git` or `sudo`, so there is a small
one-time bootstrap step inside the VM before cloning this repo and running
`./install`. Do that during the initial install flow before you switch to
disposable `run` boots.

As `root` in the guest:

```bash
apt-get update
apt-get install -y git sudo
usermod -aG sudo <bootstrap-user>
```

Then log in again as that bootstrap user and continue with the normal client
flow:

```bash
git clone <repo-url>
cd sensos-client
./install
```

The install flow adds that bootstrap user to `sensos-data`, so after `./install`
completes, log out and back in again if you want the new group membership in
that shell before generating test audio directly into `/sensos/data`.

Because `run` is disposable, anything you want to keep should be completed
during either the initial install flow or a later `update` boot, and any
persistent boot must end with a clean guest shutdown.

## Data disk

The helper always attaches a second virtual disk for testing `config-storage`.

It creates this image on demand if it does not exist:

`test/qemu/artifacts/images/debian-trixie-arm64-data.qcow2`

Inside the guest, it will typically appear as another block device such as `/dev/vdb`.

## Connectivity

The script forwards host port `2222` to guest SSH:

```bash
ssh -p 2222 <user>@127.0.0.1
```

You can add extra host port forwards with `SENSOS_QEMU_EXTRA_HOST_FWD`. Use a
comma-separated list of raw QEMU `hostfwd` rules, for example:

```bash
SENSOS_QEMU_EXTRA_HOST_FWD='tcp:0.0.0.0:8765-:8765' test/qemu/run-debian-trixie-arm64 run
```

That is useful when running two disposable VMs and you need one VM to expose a
service to the host so the other VM can reach it through `10.0.2.2`.

QEMU `hostfwd` can forward both TCP and UDP. For SensOS client/server tests,
keep the setup API and WireGuard endpoint model distinct:

- setup API from the client guest to the server guest: `10.0.2.2:18765`
- first published WireGuard endpoint from the client guest to the server guest:
  `10.0.2.2:51281`
- additional test networks: `10.0.2.2:51282` through `10.0.2.2:51289`

With QEMU user networking, the guest can usually reach macOS-hosted services at:

```text
10.0.2.2
```

That is the address to use from the guest when testing a config server running
on the host.

If the real SensOS server is remote, you can forward its config port to your
local machine first, then let the guest reach that forwarded port through
`10.0.2.2`.

Example from the macOS host:

```bash
ssh -L 8765:localhost:8765 <server>
```

Then inside the guest:

```bash
config-network --config-server 10.0.2.2 --network testing
```

In that direct-host case, the server should still publish the real reachable
WireGuard UDP endpoint for the selected network. For the first test network in
the standard SensOS QEMU workflow, that endpoint is expected to be
`10.0.2.2:51281`.

If the config server is another QEMU guest started by the helper, launch that
server VM with an extra forward first, for example:

```bash
SENSOS_QEMU_SSH_PORT=2223 \
SENSOS_QEMU_EXTRA_HOST_FWD='tcp:0.0.0.0:18765-:8765,udp:0.0.0.0:51281-:51281,udp:0.0.0.0:51282-:51282,udp:0.0.0.0:51283-:51283,udp:0.0.0.0:51284-:51284,udp:0.0.0.0:51285-:51285,udp:0.0.0.0:51286-:51286,udp:0.0.0.0:51287-:51287,udp:0.0.0.0:51288-:51288,udp:0.0.0.0:51289-:51289' \
test/qemu/run-debian-trixie-arm64 run
```

Then the client VM can enroll through the setup API with:

```bash
config-network --config-server 10.0.2.2 --setup-port 18765 --config-port 8765 --network testing
```

If you omit `--config-port`, the client now still stores steady-state API port
`8765` in `/sensos/etc/network.conf`. The setup API port `18765` is only for
enrollment. `--port` still works as a backward-compatible alias for
`--setup-port`.

In the standard QEMU flow, do not override `--wg-endpoint` just to translate an
old internal container port like `15182`. The server should publish the
host-reachable endpoint directly, which is `10.0.2.2:51281` for the first test
network unless the server explicitly returns a different valid port.

After enrollment in the standard QEMU flow, the resulting split should be:

- setup API during enrollment: `10.0.2.2:18765`
- WireGuard peer endpoint: `10.0.2.2:51281`
- steady-state API over WireGuard: `10.254.0.1:8765`

After install, run the deployed config commands as `sensos-admin`, for example:

```bash
sudo -i -u sensos-admin
```

or:

```bash
sudo -u sensos-admin config-<script>
```

To generate synthetic queued WAV files for BirdNET testing inside the guest:

```bash
python3 /path/to/repo/test/generate-queued-wav --count 3 --preset birdish
```

That writes files under:

```text
/sensos/data/audio_recordings/queued/YYYY/MM/DD/
```

Use `--preset mixed` or `--preset noise` if you want simpler non-birdlike input.

## Installer display

The launcher attaches a virtio GPU plus USB keyboard and tablet so the Debian installer appears in the QEMU window on macOS. If you ever land in the QEMU monitor instead of the guest display, try:

```text
Ctrl-Alt-1
```

to switch back to the guest console.
