# Debug Services

`debug-services` prints one-line status summaries for the SensOS systemd units
installed on the device.

Typical use:

```sh
debug-services
```

Example output shape:

```text
sensos-arecord.service              enabled=enabled    active=active    sub=running      load=loaded     SensOS audio recorder
sensos-send-status-update.timer     enabled=enabled    active=active    sub=waiting      load=loaded     Periodic SensOS status updates
sensos-hotspot.service              enabled=disabled   active=inactive  sub=dead         load=loaded     SensOS Wi-Fi hotspot
```

Use it when you want a quick at-a-glance view of which SensOS services or
timers are enabled, active, missing, or failed without paging through full
`systemctl status` output.

The command uses a fixed SensOS unit inventory, so it still prints a line for a
unit even if that unit is currently disabled or not present on the machine.
