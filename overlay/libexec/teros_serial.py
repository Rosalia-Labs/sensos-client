#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""TEROS 12 USB node adapter for the SensOS I2C reading pipeline.

A TEROS node is an Arduino Nano running teros12_nano_pnp (v3+) that fronts one
to nine METER TEROS 12 probes over SDI-12 and speaks newline-delimited JSON
over USB serial. This module makes each probe on that node look like one more
sensor to ``read-i2c-sensors.py``: a ``(key, device_address, sensor_type,
read_func)`` tuple whose ``read_func`` returns ``{key: float}``.

Why this shape
--------------
SensOS's "I2C" pipeline is a generic numeric-readings table. The one hard
constraint is server-side: ``device_address`` must be a ``0x``-prefixed hex
string. Each probe therefore gets a synthetic hex address (see
``device_address_for``) that cannot collide with the 7-bit I2C space
(0x08..0x77). Everything downstream (retries, subsample averaging, SQLite,
upload, server, public UI) is reused untouched.

The node's own SDI-12 address and the probe's METER serial number are logged
at startup so the physical mapping is always recoverable from the journal.

Standalone use (no SensOS needed), also what ``debug-teros`` calls::

    python teros_serial.py --device /dev/ttyUSB0 inventory
    python teros_serial.py --device /dev/ttyUSB0 read 1
    python teros_serial.py --device /dev/ttyUSB0 watch
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from typing import Any

import serial  # pyserial (listed in requirements-i2c.txt)

# ---- Protocol constants (match teros12_nano_pnp.ino) -----------------------

BAUD = 115200
SENSOR_TYPE = "TEROS12"
# Keys the reader stores; the node emits more fields (hints) that we drop.
READING_KEYS = ("raw", "temp_c", "ec")

# The Nano resets when the port opens and needs boot + bus scan before it can
# answer. Discovery at boot is ~0.3 s per scanned address across 0..9 plus
# commissioning, so 8 s is a safe ceiling that still fails fast when unplugged.
BOOT_WAIT_S = 8.0
# One R<addr> round trip is an SDI-12 aM!/aD0! pair, ~1.5 s worst case.
READ_TIMEOUT_S = 5.0
# Per-line serial read timeout; keeps loops responsive to an unplug.
LINE_TIMEOUT_S = 1.0

# Synthetic device_address layout: 0x1<node><sdi12 addr>. The leading 1 puts
# every probe above 0x77 so it can never shadow a real I2C device; the middle
# digit distinguishes multiple nodes on one Pi; the last is the SDI-12 address.
DEVICE_ADDRESS_PREFIX = "0x1"


def device_address_for(node_index: int, sdi12_addr: str) -> str:
    """Return the SensOS device_address for a probe.

    Parameters
    ----------
    node_index:
        0-based index of the USB node on this Pi (0 for a single node).
    sdi12_addr:
        The probe's SDI-12 address character, '1'..'9'.
    """
    if not (0 <= node_index <= 9):
        raise ValueError("node_index must be 0..9 to fit one hex digit")
    if len(sdi12_addr) != 1 or not sdi12_addr.isalnum():
        raise ValueError(f"bad SDI-12 address: {sdi12_addr!r}")
    return f"{DEVICE_ADDRESS_PREFIX}{node_index}{sdi12_addr}"


class TerosNode:
    """One USB-attached TEROS node. Owns the serial port and the inventory."""

    def __init__(self, device: str, baud: int = BAUD) -> None:
        self.device = device
        self.baud = baud
        self._ser: serial.Serial | None = None
        self._inventory: list[dict[str, str]] = []

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> None:
        """Open the port, wait for the node to boot, silence the stream, and
        fetch the inventory. Safe to call again after a failure."""
        self.close()
        # Opening toggles DTR, which resets the Nano; give it time to boot
        # before expecting anything but the hello banner.
        self._ser = serial.Serial(self.device, self.baud, timeout=LINE_TIMEOUT_S)
        self._wait_for("hello", BOOT_WAIT_S)
        # Stream mode would interleave unsolicited data with our replies, so
        # switch the node to pure request/response for the life of this handle.
        self._send("S0")
        self._wait_for("ack", READ_TIMEOUT_S)
        self.refresh_inventory()

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    # -- protocol -----------------------------------------------------------

    def _send(self, command: str) -> None:
        assert self._ser is not None
        self._ser.reset_input_buffer()
        self._ser.write(f"{command}\n".encode("ascii"))
        self._ser.flush()

    def _read_record(self) -> dict[str, Any] | None:
        """Read one line and parse it; None on timeout or non-JSON noise."""
        assert self._ser is not None
        line = self._ser.readline().decode("ascii", errors="replace").strip()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            # A partial first line right after reset is normal; skip it.
            return None

    def _wait_for(self, record_type: str, timeout_s: float) -> dict[str, Any]:
        """Read records until one of the wanted type arrives, or raise."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            rec = self._read_record()
            if rec and rec.get("type") == record_type:
                return rec
        raise TimeoutError(f"{self.device}: no '{record_type}' within {timeout_s}s")

    def refresh_inventory(self) -> list[dict[str, str]]:
        """Ask the node which addresses are occupied and by which probe."""
        self._send("?")
        rec = self._wait_for("inventory", READ_TIMEOUT_S)
        self._inventory = list(rec.get("sensors", []))
        return self._inventory

    @property
    def inventory(self) -> list[dict[str, str]]:
        return self._inventory

    def read(self, sdi12_addr: str) -> dict[str, float] | None:
        """Pull one fresh reading from a probe.

        Returns the three native values keyed as SensOS expects, or None if
        the node reported an error or went quiet. Reconnects once on a dead
        port so a transient USB hiccup does not poison every later poll.
        """
        if not self.is_open:
            try:
                self.open()
            except (serial.SerialException, OSError, TimeoutError):
                return None
        try:
            self._send(f"R{sdi12_addr}")
            deadline = time.monotonic() + READ_TIMEOUT_S
            while time.monotonic() < deadline:
                rec = self._read_record()
                if not rec or rec.get("addr") != sdi12_addr:
                    continue
                if rec.get("type") == "data":
                    return {k: float(rec[k]) for k in READING_KEYS if k in rec}
                if rec.get("type") == "error":
                    return None
            return None
        except (serial.SerialException, OSError):
            # Port vanished mid-read; drop the handle so the next call reopens.
            self.close()
            return None


# ---- SensOS registration ---------------------------------------------------

SensorEntry = tuple[str, str, str, Callable[..., dict[str, float] | None]]


def discover_teros_sensors(device: str, node_index: int = 0) -> list[SensorEntry]:
    """Open a node and return one SensOS sensor entry per discovered probe.

    Each entry is ``(key, device_address, sensor_type, read_func)`` exactly as
    ``read-i2c-sensors.py`` consumes it. All entries share the key ``TEROS`` so
    a single ``TEROS_INTERVAL_SEC`` governs every probe on the node.

    On any failure (device absent, node silent) returns an empty list so the
    caller registers nothing rather than crashing the whole reader.
    """
    node = TerosNode(device)
    try:
        node.open()
    except (serial.SerialException, OSError, TimeoutError) as exc:
        print(f"TEROS node {device}: not available ({exc})", file=sys.stderr)
        return []

    entries: list[SensorEntry] = []
    for probe in node.inventory:
        addr = str(probe.get("addr", "")).strip()
        if not addr:
            continue
        dev_addr = device_address_for(node_index, addr)

        # Bind the probe address now; the poller passes device_address as the
        # positional arg, which we accept and ignore to match read_func(addr).
        def make_reader(a: str) -> Callable[..., dict[str, float] | None]:
            return lambda _device_address=None: node.read(a)

        entries.append(("TEROS", dev_addr, SENSOR_TYPE, make_reader(addr)))
        print(
            f"TEROS node {device}: SDI-12 addr {addr} "
            f"(model {probe.get('model', '?')}, serial {probe.get('serial') or 'n/a'}) "
            f"-> device_address {dev_addr}"
        )

    if not entries:
        print(f"TEROS node {device}: no probes discovered", file=sys.stderr)
    return entries


# ---- Standalone CLI --------------------------------------------------------

def _cli() -> int:
    parser = argparse.ArgumentParser(description="Query a TEROS 12 USB node.")
    parser.add_argument("--device", required=True, help="serial device, e.g. /dev/ttyUSB0")
    parser.add_argument("--node-index", type=int, default=0, help="node index for device_address")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inventory", help="list occupied addresses and probe serials")
    p_read = sub.add_parser("read", help="read one probe now")
    p_read.add_argument("addr", help="SDI-12 address, e.g. 1")
    sub.add_parser("watch", help="poll every probe repeatedly")
    args = parser.parse_args()

    node = TerosNode(args.device)
    try:
        node.open()
    except (serial.SerialException, OSError, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.cmd == "inventory":
            if not node.inventory:
                print("no probes discovered")
                return 2
            print(f"{'sdi12':>5}  {'device_address':<14}  {'model':<7}  {'fw':<4}  serial")
            for p in node.inventory:
                print(
                    f"{p.get('addr', '?'):>5}  "
                    f"{device_address_for(args.node_index, p['addr']):<14}  "
                    f"{p.get('model', '?'):<7}  {p.get('sensor_fw', '?'):<4}  "
                    f"{p.get('serial') or 'n/a'}"
                )
            return 0
        if args.cmd == "read":
            print(json.dumps(node.read(args.addr)))
            return 0
        if args.cmd == "watch":
            while True:
                for p in node.inventory:
                    print(p["addr"], json.dumps(node.read(p["addr"])))
                time.sleep(2)
    except KeyboardInterrupt:
        return 0
    finally:
        node.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
