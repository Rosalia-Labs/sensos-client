# SPDX-License-Identifier: MIT
"""Tests for the TEROS 12 USB node adapter (overlay/libexec/teros_serial.py).

A fake node speaking the teros12_nano_pnp v3 protocol runs on a pseudo-terminal
so the real serial code path is exercised without hardware. Rows produced by
the poller's flatten step are checked against the server's device_address
contract (0x-prefixed hex), which is the one constraint that would otherwise
block an upload batch.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import threading

try:
    import pty  # POSIX only; the fake node needs a pseudo-terminal
except ImportError:  # pragma: no cover
    pty = None
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OVERLAY_ROOT = REPO_ROOT / "overlay"
os.environ["SENSOS_CLIENT_ROOT"] = str(OVERLAY_ROOT)

try:
    import serial  # noqa: F401  (pyserial, from requirements-i2c.txt)

    HAVE_PYSERIAL = True
except ImportError:  # pragma: no cover
    HAVE_PYSERIAL = False


def load_module(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


INVENTORY = [
    {"addr": "1", "model": "TER12", "sensor_fw": "206", "serial": "12345678"},
    {"addr": "2", "model": "TER12", "sensor_fw": "206", "serial": "87654321"},
]
READINGS = {"1": (1815.1, 24.6, 0.0), "2": (2740.0, 24.55, 210.0)}


def run_fake_node(master_fd: int) -> None:
    """Minimal v3 node: hello on boot, then ?/S0/R<a> request-response."""

    def emit(obj: dict) -> None:
        os.write(master_fd, (json.dumps(obj) + "\n").encode())

    # tearDown closes the master fd; any OSError from that point means the
    # test is over, so the thread exits quietly instead of tracing.
    try:
        time.sleep(0.2)
        emit({"type": "hello", "fw": "teros_pnp_v3", "n_sensors": 2, "addrs": "1,2", "stream": 1})
        buf = b""
        while True:
            chunk = os.read(master_fd, 64)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = line.decode().strip()
                if cmd == "?":
                    emit({"type": "inventory", "n_sensors": 2, "sensors": INVENTORY})
                elif cmd in ("S0", "S1"):
                    emit({"type": "ack", "cmd": cmd, "stream": int(cmd[1])})
                elif len(cmd) == 2 and cmd[0] == "R" and cmd[1] in READINGS:
                    raw, temp_c, ec = READINGS[cmd[1]]
                    emit({"type": "data", "seq": 1, "t_ms": 1, "addr": cmd[1],
                          "raw": raw, "temp_c": temp_c, "ec": ec})
                else:
                    emit({"type": "error", "t_ms": 1, "msg": "unknown_command"})
    except OSError:
        return


@unittest.skipUnless(HAVE_PYSERIAL and pty is not None, "needs pyserial and a POSIX pty")
class TerosSerialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.teros = load_module("teros_serial", OVERLAY_ROOT / "libexec" / "teros_serial.py")
        # Fast timeouts: the fake node answers immediately.
        self.teros.BOOT_WAIT_S = 3.0
        self.teros.READ_TIMEOUT_S = 2.0
        master, slave = pty.openpty()
        self.master = master
        self.device = os.ttyname(slave)
        threading.Thread(target=run_fake_node, args=(master,), daemon=True).start()

    def tearDown(self) -> None:
        os.close(self.master)

    def test_device_address_is_server_valid_hex(self) -> None:
        addr = self.teros.device_address_for(0, "1")
        self.assertEqual(addr, "0x101")
        self.assertTrue(addr.startswith("0x"))
        self.assertGreater(int(addr[2:], 16), 0x77, "must sit above the 7-bit I2C range")

    def test_discovery_registers_one_entry_per_probe(self) -> None:
        entries = self.teros.discover_teros_sensors(self.device, node_index=0)
        self.assertEqual([e[1] for e in entries], ["0x101", "0x102"])
        for key, _addr, sensor_type, read_func in entries:
            self.assertEqual(key, "TEROS")
            self.assertEqual(sensor_type, "TEROS12")
            self.assertTrue(callable(read_func))

    def test_read_func_returns_native_keys(self) -> None:
        entries = self.teros.discover_teros_sensors(self.device)
        _key, dev_addr, _type, read_func = entries[0]
        data = read_func(dev_addr)  # poller passes device_address positionally
        self.assertEqual(set(data), {"raw", "temp_c", "ec"})
        self.assertAlmostEqual(data["raw"], 1815.1)

    def test_absent_device_registers_nothing(self) -> None:
        entries = self.teros.discover_teros_sensors("/dev/does-not-exist-teros")
        self.assertEqual(entries, [])


if __name__ == "__main__":
    unittest.main()
