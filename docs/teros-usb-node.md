# TEROS USB Node

This document describes how a USB-attached TEROS 12 node joins the I2C reading
pipeline. The node is an Arduino Nano running the `teros12_nano_pnp` firmware
(v3 or later), fronting one to nine METER TEROS 12 probes over SDI-12 and
speaking newline-delimited JSON over USB serial.

## How it fits

The I2C reading pipeline is a generic numeric-readings path: the reader polls
sensors into `i2c_readings.db`, and `sensos-upload-i2c.service` ships rows to
the server. A TEROS node is registered into that same path at reader startup.
Nothing in the storage, upload, or server code is specific to a sensor type.

- Each probe on the node is its own sensor with `sensor_type` `TEROS12`.
- `device_address` is synthetic hex, `0x1<node><sdi12 addr>`: the probe at
  SDI-12 address 1 on node 0 is `0x101`. The leading `1` keeps every probe
  above the 7-bit I2C range so it can never shadow a real I2C device, and the
  server accepts it because it validates `device_address` as `0x` hex.
- Keys per probe are `raw` (calibrated dielectric counts, the source of truth
  for water content), `temp_c`, and `ec` (bulk electrical conductivity,
  microsiemens per centimetre). Water-content calibration is applied
  downstream from `raw`, not on the client.
- All probes share the `TEROS` interval key, so `TEROS_INTERVAL_SEC` (or the
  global `INTERVAL_SEC`) governs the whole node. Subsample averaging applies
  as for any other sensor.

## Runtime behavior

1. On USB plug, the udev rule in `/sensos/etc/99-sensos-teros.rules` names the
   node `/dev/teros-node-0` and restarts `sensos-read-i2c.service`.
2. The reader opens the port (the node resets on open), waits for its boot
   banner, silences the node's stream mode, and requests the inventory.
3. One sensor is registered per probe. The address-to-serial-to-
   `device_address` mapping is written to the journal.
4. The reader polls each probe on its interval with an on-demand read.

If the device is absent at startup the reader registers nothing for it and
runs as before. A node plugged in later is picked up by the udev restart.

## Setup

```sh
config-i2c-sensors --interval 300 --teros-device /dev/teros-node-0 --teros-interval 300 --start-service
```

This can be combined with the usual I2C flags. `pyserial` is installed with
the other I2C Python dependencies.

## Checking a node

```sh
debug-teros
```

prints which SDI-12 addresses are occupied and by which probe (model,
firmware, METER serial), the `device_address` each is registered under, and
recent TEROS log lines. The same query works without SensOS:

```sh
/sensos/python/venv/bin/python /sensos/libexec/teros_serial.py --device /dev/teros-node-0 inventory
```

## Probe addressing

SDI-12 cannot separate two probes that share an address, and every TEROS ships
at address 0. Commission probes one at a time: the firmware moves a lone probe
at address 0 to the lowest free address (1..9) and the probe stores it
permanently. Two unaddressed probes powered up together are reported by the
node as `collision_at_address_0`.

## More than one node

`TEROS_DEVICE` names one node. A second node needs a second
`discover_teros_sensors(path, node_index=1)` call in the reader and a udev
rule keyed on the physical USB port (`KERNELS=="1-1.2"`) with its own symlink.
Its probes register as `0x111`, `0x112`, and so on.

## Hardware and firmware

The node hardware, firmware source, probe wiring, and the full serial protocol
are documented with the firmware (`teros12_nano_pnp.ino`) in the Project Trees
repository.
