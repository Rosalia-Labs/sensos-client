#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rosalia Labs LLC

"""Reads METER TEROS 12 probes from a USB-attached node and stores them in
the same shared readings table read-i2c-sensors.py writes to.

TEROS is USB/serial, not I2C -- it gets its own config (teros.conf), its own
service (sensos-read-teros.service), and its own requirements
(requirements-teros.txt), with device discovery and error-recovery entirely
owned by teros_serial.py (a dead I2C bus and a dead serial port fail and
recover in completely different ways). What it deliberately does share is
the generic scheduling/averaging/storage machinery in sensor_polling.py and
the sensos.i2c_readings table itself (via i2c_data.py) -- that part of the
pipeline was never actually I2C-specific, just historically named for the
first sensor family that used it; duplicating it for a second transport
would just be reinventing something that already works. See
teros_serial.py's module docstring for the server-side device_address
constraint that makes sharing the table safe.
"""

import importlib.util
import os
import sys
from pathlib import Path

SCRIPT_FILE = os.path.realpath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_FILE)
OVERLAY_ROOT = os.environ.get("SENSOS_CLIENT_ROOT", "/sensos")
CLIENT_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", OVERLAY_ROOT))
UTILS_FILE = os.path.join(str(CLIENT_ROOT), "libexec", "utils.py")
sys.path.insert(0, SCRIPT_DIR)

from i2c_data import connect_db, ensure_schema
from sensor_polling import get_interval, get_subsamples_per_interval, run_polling_loop
from teros_serial import discover_teros_sensors

if not os.path.isfile(UTILS_FILE):
    raise RuntimeError(f"Missing utils.py at {UTILS_FILE}")

UTILS_SPEC = importlib.util.spec_from_file_location("sensos_overlay_utils", UTILS_FILE)
UTILS_MODULE = importlib.util.module_from_spec(UTILS_SPEC)
assert UTILS_SPEC.loader is not None
UTILS_SPEC.loader.exec_module(UTILS_MODULE)

read_kv_config = UTILS_MODULE.read_kv_config
setup_logging = UTILS_MODULE.setup_logging
ensure_runtime_dir = UTILS_MODULE.ensure_runtime_dir

CONFIG_PATH = CLIENT_ROOT / "etc" / "teros.conf"
config = read_kv_config(str(CONFIG_PATH))
if not config:
    print(f"Config file missing or empty: {CONFIG_PATH}", file=sys.stderr)
    sys.exit(1)
ensure_runtime_dir(CLIENT_ROOT / "data" / "microenv")


def store_readings(readings):
    if not readings:
        return
    try:
        with connect_db() as conn:
            conn.executemany(
                """
                INSERT INTO i2c_readings (timestamp, device_address, sensor_type, key, value)
                VALUES (?, ?, ?, ?, ?)
                """,
                readings,
            )
            conn.commit()
        print(f"Stored {len(readings)} readings.")
    except Exception as exc:
        print(f"Failed to store readings: {exc}", file=sys.stderr)


def main():
    setup_logging("read_teros_sensors.log")
    with connect_db() as conn:
        ensure_schema(conn)

    device = config.get("TEROS_DEVICE", "").strip()
    if not device:
        print(f"TEROS_DEVICE not set in {CONFIG_PATH}. Exiting.", file=sys.stderr)
        sys.exit(1)

    entries = discover_teros_sensors(device)
    if not entries:
        print(f"No TEROS probes discovered on {device}. Exiting.", file=sys.stderr)
        sys.exit(1)

    base_interval = get_interval(config, "TEROS_INTERVAL_SEC")
    if base_interval is None:
        print("TEROS_INTERVAL_SEC (or INTERVAL_SEC) not set or <= 0. Exiting.", file=sys.stderr)
        sys.exit(1)

    polling_sensors = [
        {
            "key": key,
            "addr": addr,
            "sensor_type": sensor_type,
            "read_func": read_func,
            "base_interval": base_interval,
        }
        for key, addr, sensor_type, read_func in entries
    ]

    run_polling_loop(polling_sensors, get_subsamples_per_interval(config), store_readings)


if __name__ == "__main__":
    main()
