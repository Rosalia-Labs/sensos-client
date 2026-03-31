#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import datetime
import importlib.util
import math
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_FILE = os.path.realpath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_FILE)
OVERLAY_ROOT = os.environ.get("SENSOS_CLIENT_ROOT", "/sensos")
CLIENT_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", OVERLAY_ROOT))
UTILS_FILE = os.path.join(str(CLIENT_ROOT), "libexec", "utils.py")

if not os.path.isfile(UTILS_FILE):
    raise RuntimeError(f"Missing utils.py at {UTILS_FILE}")

UTILS_SPEC = importlib.util.spec_from_file_location("sensos_overlay_utils", UTILS_FILE)
UTILS_MODULE = importlib.util.module_from_spec(UTILS_SPEC)
assert UTILS_SPEC.loader is not None
UTILS_SPEC.loader.exec_module(UTILS_MODULE)

read_kv_config = UTILS_MODULE.read_kv_config
setup_logging = UTILS_MODULE.setup_logging
create_dir = UTILS_MODULE.create_dir
write_file = UTILS_MODULE.write_file

CONFIG_PATH = CLIENT_ROOT / "etc" / "gps.conf"
LOCATION_CONF = CLIENT_ROOT / "etc" / "location.conf"
STATE_DIR = CLIENT_ROOT / "data" / "microenv"
STATE_PATH = STATE_DIR / "gps-state.env"
DEFAULT_INTERVAL_SEC = 60
DEFAULT_ADDR = "0x10"
DEFAULT_BUS = 1
DEFAULT_LOCATION_DRIFT_M = 50.0
DEFAULT_TIME_CONFLICT_SEC = 300.0
ERROR_SLEEP_SEC = 15


class TimeConflictError(RuntimeError):
    pass


def config_value(config: dict[str, str], key: str, default: str = "") -> str:
    return config.get(key, default).strip()


def config_bool(config: dict[str, str], key: str, default: bool) -> bool:
    value = config_value(config, key, "true" if default else "false").lower()
    return value in {"1", "true", "yes", "on"}


def config_int(config: dict[str, str], key: str, default: int) -> int:
    try:
        return int(config_value(config, key, str(default)))
    except ValueError:
        return default


def config_float(config: dict[str, str], key: str, default: float) -> float:
    try:
        return float(config_value(config, key, str(default)))
    except ValueError:
        return default


def timedatectl_value(key: str, default: str = "") -> str:
    proc = subprocess.run(
        ["timedatectl", "show", "-p", key, "--value"],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return default
    return proc.stdout.strip() or default


def system_time_synchronized() -> bool:
    return timedatectl_value("SystemClockSynchronized", "").lower() == "yes" or \
        timedatectl_value("NTPSynchronized", "").lower() == "yes"


def current_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * radius_m * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def read_location() -> tuple[float | None, float | None]:
    config = read_kv_config(str(LOCATION_CONF))
    try:
        return float(config["LATITUDE"]), float(config["LONGITUDE"])
    except (KeyError, ValueError):
        return None, None


def write_location(latitude: float, longitude: float) -> None:
    create_dir(str(LOCATION_CONF.parent), owner="sensos-admin", group="sensos-data", mode=0o2775)
    content = f"LATITUDE={latitude:.6f}\nLONGITUDE={longitude:.6f}\n"
    write_file(str(LOCATION_CONF), content, mode=0o664, user="sensos-admin", group="sensos-data")
    print(f"Updated location.conf to ({latitude:.6f}, {longitude:.6f})")


def state_value(value: object) -> str:
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return str(value)


def write_state(status: str, message: str, fix: dict[str, object] | None = None) -> None:
    create_dir(str(STATE_DIR), owner="sensos-admin", group="sensos-data", mode=0o2775)
    previous = read_kv_config(str(STATE_PATH))
    lines = []
    lines.append(f"STATUS={status}")
    lines.append(f"MESSAGE={message}")
    if fix is not None:
        for key in ("latitude", "longitude", "altitude", "fix", "source", "gps_time"):
            value = fix.get(key)
            if value is not None:
                lines.append(f"{key.upper()}={state_value(value)}")
        for key in ("latitude", "longitude", "altitude", "fix", "source", "gps_time"):
            value = fix.get(key)
            if value is not None:
                lines.append(f"LAST_FIX_{key.upper()}={state_value(value)}")
        lines.append(
            f"LAST_FIX_AT={current_utc().replace(microsecond=0).isoformat().replace('+00:00', 'Z')}"
        )
    else:
        for key in (
            "LAST_FIX_LATITUDE",
            "LAST_FIX_LONGITUDE",
            "LAST_FIX_ALTITUDE",
            "LAST_FIX_FIX",
            "LAST_FIX_SOURCE",
            "LAST_FIX_GPS_TIME",
            "LAST_FIX_AT",
        ):
            value = previous.get(key)
            if value:
                lines.append(f"{key}={value}")
    lines.append(f"UPDATED_AT={current_utc().replace(microsecond=0).isoformat().replace('+00:00', 'Z')}")
    write_file(str(STATE_PATH), "\n".join(lines) + "\n", mode=0o664, user="sensos-admin", group="sensos-data")


def set_system_time(gps_time: datetime.datetime) -> None:
    timestamp = gps_time.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
    proc = subprocess.run(
        ["sudo", "timedatectl", "set-time", timestamp],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "failed to set system time from GPS")
    print(f"Updated system UTC time from GPS to {timestamp}")


def parse_i2c_gps(bus_num: int, addr_str: str) -> dict[str, object] | None:
    import pynmea2
    import smbus2

    i2c_addr = int(addr_str, 16)
    with smbus2.SMBus(bus_num) as bus:
        available = bus.read_byte_data(i2c_addr, 0xFD)
        if available == 0:
            return None
        raw_chars = [chr(bus.read_byte_data(i2c_addr, 0xFF)) for _ in range(available)]

    nmea = "".join(raw_chars)
    last_rmc = None
    last_gga = None
    for line in nmea.splitlines():
        if not line.startswith("$GP"):
            continue
        try:
            msg = pynmea2.parse(line)
        except pynmea2.ParseError:
            continue
        sentence = getattr(msg, "sentence_type", "")
        if sentence == "RMC":
            last_rmc = msg
        elif sentence == "GGA":
            last_gga = msg

    fix_quality = getattr(last_gga, "gps_qual", None)
    if fix_quality and str(fix_quality).isdigit():
        fix = int(fix_quality)
    else:
        fix = 1 if getattr(last_rmc, "status", "") == "A" else 0
    if fix <= 0:
        return None

    latitude = getattr(last_rmc, "latitude", None) or getattr(last_gga, "latitude", None)
    longitude = getattr(last_rmc, "longitude", None) or getattr(last_gga, "longitude", None)
    if latitude in (None, "") or longitude in (None, ""):
        return None

    altitude = getattr(last_gga, "altitude", None)
    gps_time = None
    if getattr(last_rmc, "datestamp", None) and getattr(last_rmc, "timestamp", None):
        gps_time = datetime.datetime.combine(
            last_rmc.datestamp,
            last_rmc.timestamp,
            tzinfo=datetime.UTC,
        )

    return {
        "latitude": float(latitude),
        "longitude": float(longitude),
        "altitude": float(altitude) if altitude not in (None, "") else None,
        "fix": fix,
        "gps_time": gps_time,
        "source": f"i2c:{addr_str}",
    }


def maybe_update_time(fix: dict[str, object], allow_sync: bool) -> None:
    if not allow_sync:
        return
    gps_time = fix.get("gps_time")
    if not isinstance(gps_time, datetime.datetime):
        return
    if system_time_synchronized():
        return
    set_system_time(gps_time)


def maybe_validate_time_source(
    fix: dict[str, object],
    conflict_threshold_sec: float,
    allow_sync: bool,
) -> None:
    if not allow_sync or not system_time_synchronized():
        return
    gps_time = fix.get("gps_time")
    if not isinstance(gps_time, datetime.datetime):
        return
    drift_sec = abs((current_utc() - gps_time).total_seconds())
    if drift_sec < conflict_threshold_sec:
        return
    raise TimeConflictError(
        "GPS time differs from the synchronized system clock by "
        f"{drift_sec:.1f}s, above the {conflict_threshold_sec:.1f}s conflict threshold"
    )


def maybe_update_location(fix: dict[str, object], threshold_m: float, allow_update: bool) -> None:
    if not allow_update:
        return
    latitude = fix.get("latitude")
    longitude = fix.get("longitude")
    if not isinstance(latitude, float) or not isinstance(longitude, float):
        return
    current_lat, current_lon = read_location()
    if current_lat is None or current_lon is None:
        write_location(latitude, longitude)
        return
    if haversine_m(current_lat, current_lon, latitude, longitude) >= threshold_m:
        write_location(latitude, longitude)


def main() -> int:
    setup_logging("sensos_gps.log")
    config = read_kv_config(str(CONFIG_PATH))
    if not config:
        print(f"GPS config missing or empty: {CONFIG_PATH}", file=sys.stderr)
        return 1

    if not config_bool(config, "GPS_ENABLED", False):
        print("GPS service disabled in gps.conf")
        return 1

    backend = config_value(config, "GPS_BACKEND", "i2c").lower()
    interval_sec = max(5, config_int(config, "GPS_INTERVAL_SEC", DEFAULT_INTERVAL_SEC))
    bus_num = config_int(config, "GPS_I2C_BUS", DEFAULT_BUS)
    addr_str = config_value(config, "GPS_I2C_ADDR", DEFAULT_ADDR)
    allow_sync = config_bool(config, "GPS_SYNC_TIME", True)
    allow_location = config_bool(config, "GPS_UPDATE_LOCATION", True)
    location_threshold_m = max(0.0, config_float(config, "GPS_LOCATION_DRIFT_M", DEFAULT_LOCATION_DRIFT_M))
    conflict_threshold_sec = max(0.0, config_float(config, "GPS_TIME_CONFLICT_SEC", DEFAULT_TIME_CONFLICT_SEC))

    print(
        f"sensos-gps starting: backend={backend} interval={interval_sec}s "
        f"sync_time={'yes' if allow_sync else 'no'} "
        f"update_location={'yes' if allow_location else 'no'}"
    )
    write_state(
        "starting",
        f"backend={backend} interval={interval_sec}s sync_time={'yes' if allow_sync else 'no'} "
        f"update_location={'yes' if allow_location else 'no'}",
    )

    while True:
        try:
            if backend != "i2c":
                message = f"Unsupported GPS backend '{backend}'"
                print(message, file=sys.stderr)
                write_state("error", message)
                time.sleep(ERROR_SLEEP_SEC)
                continue
            fix = parse_i2c_gps(bus_num, addr_str)
            if fix is None:
                message = "No valid GPS fix available."
                print(message)
                write_state("no_fix", message)
                time.sleep(interval_sec)
                continue
            message = f"GPS fix: lat={fix['latitude']:.6f} lon={fix['longitude']:.6f} source={fix['source']}"
            print(message)
            maybe_validate_time_source(fix, conflict_threshold_sec, allow_sync)
            maybe_update_time(fix, allow_sync)
            maybe_update_location(fix, location_threshold_m, allow_location)
            write_state("fix", message, fix)
            time.sleep(interval_sec)
        except TimeConflictError as exc:
            message = f"GPS time conflict: {exc}"
            print(message, file=sys.stderr)
            write_state("time_conflict", message)
            time.sleep(interval_sec)
        except Exception as exc:
            message = f"GPS service failure: {exc}"
            print(message, file=sys.stderr)
            write_state("error", message)
            time.sleep(ERROR_SLEEP_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
