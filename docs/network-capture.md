# Network Capture

SensOS can run a rotating packet capture session for debugging and generate top-N reports from that session.

## What it captures

- link-layer packet headers using `tcpdump`
- all interfaces by default via `-i any`
- truncated packets with a default snap length of 128 bytes

This is enough to identify sustained traffic sources and destinations, common ports, and high-volume flows without trying to store full packet payloads.

## Session workflow

Start a debug session on the device:

```sh
network-capture start
```

That creates a session under `/sensos/log/network_capture/sessions/<timestamp>` and starts [sensos-network-capture.service](../overlay/systemd/sensos-network-capture.service) manually for that session only. The service is not enabled during normal install.

Privilege model:

- use `network-capture` for normal operations
- `network-capture start`, `status`, and `stop` handle `sudo` internally for service control
- packet capture itself runs as `sensos-runner:sensos-data`, not root
- the service receives only `CAP_NET_ADMIN` and `CAP_NET_RAW` for packet capture
- `network-capture report` is read-only and does not self-elevate
- saved reports and retained session data are kept writable for the shared admin/data group so `network-capture report --save`, `network-capture report --cleanup`, and `network-capture cleanup` do not require you to guess which helper needs `sudo`

The capture keeps running until you stop it:

```sh
network-capture stop
```

Check status:

```sh
network-capture status
```

Generate reports from the latest session and remove the raw packet captures after reporting:

```sh
network-capture report --latest --cleanup
```

If you also want to save text and JSON report files under the session directory:

```sh
network-capture report --latest --save
```

If you want to remove the generated summary reports too:

```sh
network-capture cleanup --latest --remove-reports
```

If you want to clear all retained capture sessions and start fresh:

```sh
network-capture cleanup --all
```

## Storage bounds

- default file size: 8 MiB
- default file count: 48
- default maximum retained raw capture: about 384 MiB

The capture ring is bounded inside the session directory. Sessions are no longer time-limited by default; `network-capture stop` is the normal way to end a capture.

The service runs `/sensos/libexec/start-network-capture.sh`, which starts a rotating `tcpdump` session similar to:

```sh
tcpdump -i any -nn -p -U -s 128 -y LINUX_SLL -C 8 -W 48 -w /sensos/log/network_capture/sessions/<timestamp>/pcap/capture.pcap
```

The exact values can be overridden through `network-capture start` or by setting environment values before starting the service:

- `SENSOS_NETWORK_CAPTURE_ROOT`
- `SENSOS_NETWORK_CAPTURE_IFACE`
- `SENSOS_NETWORK_CAPTURE_SNAPLEN`
- `SENSOS_NETWORK_CAPTURE_FILE_MB`
- `SENSOS_NETWORK_CAPTURE_FILE_COUNT`
- `SENSOS_NETWORK_CAPTURE_BUFFER_KIB`

## Reports

`network-capture report` drives the analyzer. Point it at the latest session
with `--latest`, or at a specific one with `--session-root`:

```sh
network-capture report --session-root /sensos/log/network_capture/sessions/<timestamp> --hours 0 --top 20
```

The report summarizes retained traffic by:

- direction
- direction and protocol
- remote peer IP
- local port
- remote port
- flow tuple: direction, protocol, local IP/port, remote IP/port

This is intended to answer questions like:

- which remote IP keeps sending traffic to the Pi?
- which local port is consuming bandwidth?
- which flow is the top sustained talker?

For machine-readable output:

```sh
network-capture report --session-root /sensos/log/network_capture/sessions/<timestamp> --hours 0 --json
```
