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
reboot, then use disposable run boots when you want a non-sticky test session:

```bash
test/qemu/run-debian-trixie-arm64 run
```

The `run` command uses `-snapshot`, so guest disk changes are discarded when
QEMU exits.

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
during the install flow before switching to `run`.

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

If the config server is another QEMU guest started by the helper, launch that
server VM with an extra forward first, for example:

```bash
SENSOS_QEMU_SSH_PORT=2223 \
SENSOS_QEMU_EXTRA_HOST_FWD='tcp:0.0.0.0:18765-:8765' \
test/qemu/run-debian-trixie-arm64 run
```

Then the client VM can still use:

```bash
config-network --config-server 10.0.2.2 --port 18765 --config-port 8765 --network testing
```

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
