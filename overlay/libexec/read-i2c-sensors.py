#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import atexit
import datetime
import heapq
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

MAX_ATTEMPTS = 3
BACKOFF_MULTIPLIER = 2

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


def get_interval(key: str) -> Optional[int]:
    value_str = config.get(key, "").strip()
    if value_str:
        try:
            value = int(value_str)
            return value if value > 0 else None
        except ValueError:
            return None
    fallback_str = config.get("INTERVAL_SEC", "").strip()
    if fallback_str:
        try:
            value = int(fallback_str)
            return value if value > 0 else None
        except ValueError:
            return None
    return None


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


def get_subsamples_per_interval() -> int:
    raw = config.get("SUBSAMPLES_PER_INTERVAL", "").strip()
    if not raw:
        return 1
    try:
        value = int(raw)
    except ValueError:
        return 1
    return max(1, value)


def average_sensor_samples(samples: list[dict]) -> Optional[dict]:
    if not samples:
        return None
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for sample in samples:
        if not sample:
            continue
        for key, value in sample.items():
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            sums[key] = sums.get(key, 0.0) + numeric
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    averaged: dict[str, float] = {}
    for key, total in sums.items():
        count = counts.get(key, 0)
        if count <= 0:
            continue
        averaged[key] = round(total / count, 3)
    return averaged or None


def read_with_retries(sensor: dict) -> Optional[dict]:
    data = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            data = safe_sensor_read(sensor["read_func"], sensor["addr"])
            if data:
                return data
            print(
                f"{sensor['sensor_type']} returned no data (attempt {attempt}/{MAX_ATTEMPTS})"
            )
        except Exception as exc:
            print(
                f"Error on attempt {attempt} reading {sensor['sensor_type']}: {exc}",
                file=sys.stderr,
            )
        time.sleep(0.2)
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


def flatten_sensor_data(sensor_data, device_address, sensor_type, timestamp):
    if not sensor_data:
        return []
    flat = []
    for key, value in sensor_data.items():
        try:
            flat.append((timestamp, device_address, sensor_type, key, float(value)))
        except (TypeError, ValueError):
            continue
    return flat


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

    polling_queue = []
    for key, addr, sensor_type, read_func in sensors:
        interval_key = f"{key}_INTERVAL_SEC"
        explicitly_configured = bool(config.get(interval_key, "").strip())
        base_interval = get_interval(interval_key)
        if base_interval is None:
            continue  # explicitly disabled (interval <= 0), or unset with no INTERVAL_SEC fallback either

        if not sensor_should_poll(explicitly_configured, int(addr, 16), scan_result):
            print(f"Skipping {sensor_type} at {addr}: not detected on the I2C bus.")
            continue

        heapq.heappush(
            polling_queue,
            (
                time.time(),
                {
                    "key": key,
                    "addr": addr,
                    "sensor_type": sensor_type,
                    "read_func": read_func,
                    "base_interval": base_interval,
                    "current_interval": base_interval,
                },
            ),
        )

    if not polling_queue:
        print("No sensors enabled. Exiting.")
        sys.exit(1)

    subsamples_per_interval = get_subsamples_per_interval()
    print(f"Using subsamples_per_interval={subsamples_per_interval}")
    print("Entering sensor loop (priority queue with retries + backoff)")
    while polling_queue:
        now = time.time()
        next_time, sensor = heapq.heappop(polling_queue)
        wait = max(0, next_time - now)
        if wait:
            time.sleep(wait)

        timestamp = datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        print(
            f"Polling {sensor['sensor_type']} at {sensor['addr']} "
            f"with {subsamples_per_interval} subsample(s)..."
        )

        sample_values: list[dict] = []
        subsample_spacing_sec = sensor["base_interval"] / max(subsamples_per_interval, 1)
        for subsample_index in range(subsamples_per_interval):
            sample = read_with_retries(sensor)
            if sample:
                sample_values.append(sample)
            if subsample_index < subsamples_per_interval - 1:
                time.sleep(subsample_spacing_sec)

        averaged_data = average_sensor_samples(sample_values)
        if averaged_data:
            print(
                f"{sensor['sensor_type']} ({sensor['addr']}) averaged "
                f"{len(sample_values)}/{subsamples_per_interval} samples: {averaged_data}"
            )
            readings = flatten_sensor_data(
                averaged_data, sensor["addr"], sensor["sensor_type"], timestamp
            )
            store_readings(readings)
            sensor["current_interval"] = sensor["base_interval"]
        else:
            print(
                f"No valid subsamples were captured for {sensor['sensor_type']} at {sensor['addr']}; backing off."
            )
            sensor["current_interval"] = min(sensor["current_interval"] * BACKOFF_MULTIPLIER, 3600)

        next_poll_time = next_time + sensor["current_interval"]
        heapq.heappush(polling_queue, (next_poll_time, sensor))


if __name__ == "__main__":
    main()
