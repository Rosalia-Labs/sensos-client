# Network Capture

SensOS can run a bounded, temporary packet capture session for debugging and generate top-N reports from that session.

## What it captures

- link-layer packet headers using `tcpdump`
- all interfaces by default via `-i any`
- truncated packets with a default snap length of 128 bytes

This is enough to identify sustained traffic sources and destinations, common ports, and high-volume flows without trying to store full packet payloads.

## Session workflow

Start a 24-hour debug session on the device:

```sh
debug-network-capture start --hours 24
```

That creates a session under `/sensos/log/network_capture/sessions/<timestamp>` and starts [sensos-network-capture.service](/Users/tkeitt/Projects/sensos-client/overlay/systemd/sensos-network-capture.service) manually for that session only. The service is not enabled during normal install.

Check status:

```sh
debug-network-capture status
```

Generate reports from the latest session and remove the raw packet captures after reporting:

```sh
debug-network-capture report --latest --cleanup
```

If you want to remove the generated summary reports too:

```sh
debug-network-capture cleanup --latest --remove-reports
```

## Storage bounds

- default file size: 8 MiB
- default file count: 48
- default maximum retained raw capture: about 384 MiB

The capture ring is bounded inside the session directory. Each session also has a fixed duration, which defaults to 24 hours.

The service runs `/sensos/libexec/start-network-capture.sh`, which starts a bounded `tcpdump` session similar to:

```sh
timeout 86400 tcpdump -i any -nn -p -s 128 -y LINUX_SLL -C 8 -W 48 -w /sensos/log/network_capture/sessions/<timestamp>/pcap/capture.pcap
```

The exact values can be overridden through `debug-network-capture start` or by setting environment values before starting the service:

- `SENSOS_NETWORK_CAPTURE_ROOT`
- `SENSOS_NETWORK_CAPTURE_IFACE`
- `SENSOS_NETWORK_CAPTURE_DURATION_SEC`
- `SENSOS_NETWORK_CAPTURE_SNAPLEN`
- `SENSOS_NETWORK_CAPTURE_FILE_MB`
- `SENSOS_NETWORK_CAPTURE_FILE_COUNT`
- `SENSOS_NETWORK_CAPTURE_BUFFER_KIB`

## Reports

Use [debug-network-capture](/Users/tkeitt/Projects/sensos-client/overlay/bin/debug-network-capture) for the full session workflow, or call [report-network-capture](/Users/tkeitt/Projects/sensos-client/overlay/bin/report-network-capture) directly against a session root:

```sh
report-network-capture --capture-root /sensos/log/network_capture/sessions/<timestamp> --hours 0 --top 20
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
report-network-capture --capture-root /sensos/log/network_capture/sessions/<timestamp> --hours 0 --json
```
