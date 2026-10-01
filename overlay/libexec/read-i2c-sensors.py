#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import atexit
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Optional

SCRIPT_FILE = os.path.realpath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_FILE)
OVERLAY_ROOT = os.environ.get("SENSOS_CLIENT_ROOT", "/sensos")
CLIENT_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", OVERLAY_ROOT))
UTILS_FILE = os.path.join(str(CLIENT_ROOT), "libexec", "utils.py")
sys.path.insert(0, SCRIPT_DIR)

from i2c_data import connect_db, ensure_schema
from sensor_polling import get_interval, get_subsamples_per_interval, run_polling_loop

if not os.path.isfile(UTILS_FILE):
    raise RuntimeError(f"Missing utils.py at {UTILS_FILE}")

UTILS_SPEC = importlib.util.spec_from_file_location("sensos_overlay_utils", UTILS_FILE)
UTILS_MODULE = importlib.util.module_from_spec(UTILS_SPEC)
assert UTILS_SPEC.loader is not None
UTILS_SPEC.loader.exec_module(UTILS_MODULE)

read_kv_config = UTILS_MODULE.read_kv_config
setup_logging = UTILS_MODULE.setup_logging
ensure_runtime_dir = UTILS_MODULE.ensure_runtime_dir

CONFIG_PATH = CLIENT_ROOT / "etc" / "i2c-sensors.conf"
config = read_kv_config(str(CONFIG_PATH))
if not config:
    print(f"Config file missing or empty: {CONFIG_PATH}", file=sys.stderr)
    sys.exit(1)
ensure_runtime_dir(CLIENT_ROOT / "data" / "microenv")

_i2c = None
_cached = {
    "bme280": {},
    "ads1015": {},
    "scd30": None,
    "scd4x": {"driver": None, "warmed": False},
}


def get_i2c(force_reset: bool = False):
    global _i2c
    try:
        if force_reset and _i2c and hasattr(_i2c, "deinit"):
            _i2c.deinit()
            _i2c = None
    except Exception:
        _i2c = None

    if _i2c is None:
        import board
        import busio

        _i2c = busio.I2C(board.SCL, board.SDA)
    return _i2c


def _register_i2c_cleanup_once():
    def _cleanup():
        try:
            if _i2c and hasattr(_i2c, "deinit"):
                _i2c.deinit()
        except Exception:
            pass

    if not getattr(_register_i2c_cleanup_once, "_done", False):
        atexit.register(_cleanup)
        _register_i2c_cleanup_once._done = True


_register_i2c_cleanup_once()


def safe_sensor_read(read_func, *args, **kwargs):
    try:
        return read_func(*args, **kwargs)
    except OSError as err:
        errno = getattr(err, "errno", None)
        message = str(err).lower()
        if errno in (121, 5) or "remote i/o" in message or "input/output error" in message:
            print("Detected I2C error; resetting bus and retrying once...", file=sys.stderr)
            get_i2c(force_reset=True)
            return read_func(*args, **kwargs)
        raise


I2C_BUS_NUM = 1


def sensor_should_poll(
    explicitly_configured: bool, addr_int: int, scan_result: Optional[set[int]]
) -> bool:
    """An explicit --<sensor>-interval always wins, in either direction
    (get_interval() already turns an explicit <=0 into base_interval=None,
    which callers filter out before ever reaching this function; an explicit
    positive value reaches here and is always polled). A sensor relying on
    the INTERVAL_SEC fallback (not explicitly configured) is scan-gated: only
    polled if the startup bus scan actually found it, unless the scan itself
    failed (scan_result is None), in which case nothing is gated."""
    if explicitly_configured:
        return True
    if scan_result is None:
        return True
    return addr_int in scan_result


def scan_i2c_addresses(candidate_addrs: set[int]) -> Optional[set[int]]:
    """Probe each candidate address for an ACK (a zero-byte "quick write",
    the same technique i2cdetect/debug-i2c use), once at startup, so a sensor
    slot with no hardware attached is never scheduled at all instead of
    burning a full interval-width polling cycle before backing off (see
    2026-09-24 field incident: an unused BME280_0x76 slot delayed the
    present BME280_0x77's first reading by a full interval).

    Returns the set of responding addresses, or None if the scan itself could
    not run (bus open failed) -- callers should treat None as "skip
    scan-gating, poll every configured sensor" rather than silently excluding
    everything, so a scan failure degrades to the pre-scan behavior instead
    of a fleet-wide zero-readings regression.
    """
    try:
        import smbus2

        detected: set[int] = set()
        with smbus2.SMBus(I2C_BUS_NUM) as bus:
            for addr in candidate_addrs:
                try:
                    bus.write_quick(addr)
                    detected.add(addr)
                except OSError:
                    continue
        return detected
    except Exception as exc:
        print(
            f"I2C bus scan failed ({exc}); polling all configured sensors without bus-presence gating.",
            file=sys.stderr,
        )
        return None


def read_bme280(addr_str: str = None):
    try:
        from adafruit_bme280.basic import Adafruit_BME280_I2C

        i2c = get_i2c()
        addr = int(addr_str, 16)
        driver = _cached["bme280"].get(addr)
        if driver is None:
            driver = Adafruit_BME280_I2C(i2c, address=addr)
            _cached["bme280"][addr] = driver
        return {
            "temperature_c": round(driver.temperature, 2),
            "humidity_percent": round(driver.humidity, 2),
            "pressure_hpa": round(driver.pressure, 2),
        }
    except Exception as exc:
        print(f"Error reading BME280@{addr_str}: {exc}", file=sys.stderr)
        return None


def read_ads1015(addr_str: str = None):
    try:
        import adafruit_ads1x15.ads1015 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn

        i2c = get_i2c()
        addr = int(addr_str, 16) if addr_str else 0x48
        ads_cache = _cached.get("ads1015")
        if not isinstance(ads_cache, dict):
            ads_cache = _cached["ads1015"] = {}

        ads = ads_cache.get(addr)
        if ads is None:
            ads = ADS.ADS1015(i2c, address=addr)
            ads_cache[addr] = ads

        return {
            "A0": round(AnalogIn(ads, 0).voltage, 3),
            "A1": round(AnalogIn(ads, 1).voltage, 3),
            "A2": round(AnalogIn(ads, 2).voltage, 3),
            "A3": round(AnalogIn(ads, 3).voltage, 3),
        }
    except Exception as exc:
        print(f"Error reading ADS1015: {exc}", file=sys.stderr)
        return None


def read_scd30(addr_str: str = None):
    try:
        import adafruit_scd30

        i2c = get_i2c()
        scd30 = _cached["scd30"]
        if scd30 is None:
            scd30 = adafruit_scd30.SCD30(i2c)
            _cached["scd30"] = scd30
        if not scd30.data_available:
            return None
        return {
            "co2_ppm": round(scd30.CO2, 1),
            "temperature_c": round(scd30.temperature, 2),
            "humidity_percent": round(scd30.relative_humidity, 2),
        }
    except Exception as exc:
        print(f"Error reading SCD30: {exc}", file=sys.stderr)
        return None


def read_scd4x(addr_str: str = None):
    try:
        import adafruit_scd4x

        i2c = get_i2c()
        state = _cached["scd4x"]
        scd = state["driver"]
        if scd is None:
            scd = adafruit_scd4x.SCD4X(i2c)
            scd.start_periodic_measurement()
            state["driver"] = scd
            time.sleep(5)
            state["warmed"] = True
        if not scd.data_ready:
            return None
        return {
            "co2_ppm": round(scd.CO2, 1),
            "temperature_c": round(scd.temperature, 2),
            "humidity_percent": round(scd.relative_humidity, 2),
        }
    except Exception as exc:
        print(f"Error reading SCD4X: {exc}", file=sys.stderr)
        return None


def read_lt150(addr_str: str = "0x49"):
    try:
        import adafruit_ads1x15.ads1015 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn

        i2c = get_i2c()
        addr = int(addr_str, 16)
        ads_cache = _cached.get("ads1015")
        if not isinstance(ads_cache, dict):
            ads_cache = _cached["ads1015"] = {}

        ads = ads_cache.get(addr)
        if ads is None:
            ads = ADS.ADS1015(i2c, address=addr)
            ads.gain = 1
            ads_cache[addr] = ads

        volts = AnalogIn(ads, 0).voltage
        lux = max(0.0, volts * 50000.0)
        return {"lux": round(lux, 1), "volts": round(volts, 3)}
    except Exception as exc:
        print(f"Error reading LT-150 @ {addr_str}: {exc}", file=sys.stderr)
        return None


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
    setup_logging("read_i2c_sensors.log")
    with connect_db() as conn:
        ensure_schema(conn)

    sensors = [
        ("BME280_0x76", "0x76", "BME280", read_bme280),
        ("BME280_0x77", "0x77", "BME280", read_bme280),
        ("ADS1015", "0x48", "ADS1015", read_ads1015),
        ("LT150", "0x49", "LT150", read_lt150),
        ("SCD30", "0x61", "SCD30", read_scd30),
        ("SCD4X", "0x62", "SCD4X", read_scd4x),
    ]

    scan_result = scan_i2c_addresses({int(addr, 16) for _, addr, _, _ in sensors})

    polling_sensors = []
    for key, addr, sensor_type, read_func in sensors:
        interval_key = f"{key}_INTERVAL_SEC"
        explicitly_configured = bool(config.get(interval_key, "").strip())
        base_interval = get_interval(config, interval_key)
        if base_interval is None:
            continue  # explicitly disabled (interval <= 0), or unset with no INTERVAL_SEC fallback either

        if not sensor_should_poll(explicitly_configured, int(addr, 16), scan_result):
            print(f"Skipping {sensor_type} at {addr}: not detected on the I2C bus.")
            continue

        # Wrap here, not inside the shared polling loop: the I2C bus-reset
        # retry is specific to this transport and has no serial equivalent
        # (see read-teros-sensors.py, which passes its read_func unwrapped).
        polling_sensors.append(
            {
                "key": key,
                "addr": addr,
                "sensor_type": sensor_type,
                "read_func": lambda a, _f=read_func: safe_sensor_read(_f, a),
                "base_interval": base_interval,
            }
        )

    if not polling_sensors:
        print("No sensors enabled. Exiting.")
        sys.exit(1)

    run_polling_loop(polling_sensors, get_subsamples_per_interval(config), store_readings)


if __name__ == "__main__":
    main()
