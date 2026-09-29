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
ensure_runtime_dir = UTILS_MODULE.ensure_runtime_dir
write_runtime_file = UTILS_MODULE.write_runtime_file
report_event = UTILS_MODULE.report_event
read_network_conf = UTILS_MODULE.read_network_conf
require_peer_uuid = UTILS_MODULE.require_peer_uuid
read_service_credential = UTILS_MODULE.read_service_credential
send_client_location = UTILS_MODULE.send_client_location

CONFIG_PATH = CLIENT_ROOT / "etc" / "gps.conf"
LOCATION_CONF = CLIENT_ROOT / "etc" / "location.conf"
STATE_DIR = CLIENT_ROOT / "data" / "microenv"
STATE_PATH = STATE_DIR / "gps-state.env"
DEFAULT_INTERVAL_SEC = 60
DEFAULT_ADDR = "0x10"
DEFAULT_BUS = 1
DEFAULT_SERIAL_BAUD = 9600
DEFAULT_SERIAL_COLLECT_SEC = 5.0
SERIAL_PORT_GLOBS = ("/dev/serial/by-id/*", "/dev/ttyACM*", "/dev/ttyUSB*")
DEFAULT_LOCATION_MOVE_THRESHOLD_M = 1000.0
DEFAULT_INITIAL_FIX_MINUTES = 5.0
INITIAL_FIX_SAMPLE_INTERVAL_SEC = 10
DEFAULT_TIME_CONFLICT_SEC = 300.0
ERROR_SLEEP_SEC = 15
MAX_NMEA_BUFFER_BYTES = 8192


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
    content = f"LATITUDE={latitude:.6f}\nLONGITUDE={longitude:.6f}\n"
    write_runtime_file(LOCATION_CONF, content)
    print(f"Updated location.conf to ({latitude:.6f}, {longitude:.6f})")


def state_value(value: object) -> str:
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return str(value)


def state_lines(
    status: str,
    message: str,
    previous: dict[str, str],
    fix: dict[str, object] | None = None,
) -> list[str]:
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
    return lines


def write_state(status: str, message: str, fix: dict[str, object] | None = None) -> None:
    ensure_runtime_dir(STATE_DIR)
    previous = read_kv_config(str(STATE_PATH))
    lines = state_lines(status, message, previous, fix)
    lines.append(f"UPDATED_AT={current_utc().replace(microsecond=0).isoformat().replace('+00:00', 'Z')}")
    write_runtime_file(STATE_PATH, "\n".join(lines) + "\n")


def set_system_time(gps_time: datetime.datetime) -> None:
    old_time = current_utc()
    timestamp = gps_time.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
    try:
        time.clock_settime(time.CLOCK_REALTIME, gps_time.timestamp())
    except (OSError, PermissionError) as exc:
        raise RuntimeError(f"failed to set system time from GPS: {exc}") from exc
    print(f"Updated system UTC time from GPS to {timestamp}")
    report_event(
        "gps_time_updated",
        severity="notice",
        details={
            "old_utc_time": state_value(old_time),
            "new_utc_time": state_value(gps_time),
            "source": "gps",
        },
        dedupe_window=300,
    )


def read_i2c_gps_chunk(bus_num: int, addr_str: str) -> str:
    import smbus2

    i2c_addr = int(addr_str, 16)
    with smbus2.SMBus(bus_num) as bus:
        available = bus.read_byte_data(i2c_addr, 0xFD)
        if available <= 0:
            return ""
        raw_chars = [chr(bus.read_byte_data(i2c_addr, 0xFF)) for _ in range(available)]
    return "".join(raw_chars)


def extract_nmea_lines(buffer: str) -> tuple[list[str], str]:
    start = buffer.find("$")
    if start > 0:
        buffer = buffer[start:]
    elif start < 0:
        return [], buffer[-MAX_NMEA_BUFFER_BYTES:]

    complete: list[str] = []
    parts = buffer.splitlines(keepends=True)
    remainder = ""
    for part in parts:
        if part.endswith("\n") or part.endswith("\r"):
            line = part.strip()
            if line:
                complete.append(line)
        else:
            remainder = part
    if len(remainder) > MAX_NMEA_BUFFER_BYTES:
        remainder = remainder[-MAX_NMEA_BUFFER_BYTES:]
        start = remainder.find("$")
        if start >= 0:
            remainder = remainder[start:]
    return complete, remainder


def parse_nmea_fix(lines: list[str], source_label: str) -> dict[str, object] | None:
    import pynmea2
    last_rmc = None
    last_gga = None
    for line in lines:
        if not line.startswith(("$GP", "$GN")):
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
        "source": source_label,
    }


def parse_i2c_gps(bus_num: int, addr_str: str, buffer: str) -> tuple[dict[str, object] | None, str]:
    chunk = read_i2c_gps_chunk(bus_num, addr_str)
    if chunk:
        buffer = (buffer + chunk)[-MAX_NMEA_BUFFER_BYTES:]
    lines, remainder = extract_nmea_lines(buffer)
    if not lines:
        return None, remainder
    return parse_nmea_fix(lines, f"i2c:{addr_str}"), remainder


def autodetect_serial_port() -> str | None:
    import glob

    for pattern in SERIAL_PORT_GLOBS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


class SerialGps:
    """Reads NMEA from a USB/UART GPS exposed as a serial character device."""

    def __init__(self, port_hint: str, baud: int) -> None:
        self.port_hint = port_hint
        self.baud = baud
        self.handle = None
        self.port: str | None = None

    def _resolve_port(self) -> str | None:
        if self.port_hint:
            return self.port_hint if os.path.exists(self.port_hint) else None
        return autodetect_serial_port()

    def close(self) -> None:
        if self.handle is not None:
            try:
                self.handle.close()
            except Exception:
                pass
        self.handle = None
        self.port = None

    def _ensure_open(self) -> None:
        if self.handle is not None:
            return
        import serial

        port = self._resolve_port()
        if not port:
            hint = self.port_hint or " or ".join(SERIAL_PORT_GLOBS)
            raise RuntimeError(f"no GPS serial device found ({hint})")
        self.handle = serial.Serial(port, baudrate=self.baud, timeout=1)
        self.port = port
        print(f"Opened GPS serial port {port} @ {self.baud} baud")

    def read_fix(self, collect_seconds: float = DEFAULT_SERIAL_COLLECT_SEC) -> dict[str, object] | None:
        self._ensure_open()
        assert self.handle is not None

        deadline = time.monotonic() + collect_seconds
        lines: list[str] = []
        have_rmc = have_gga = False
        while time.monotonic() < deadline:
            raw = self.handle.readline()
            if not raw:
                continue
            line = raw.decode("ascii", errors="ignore").strip()
            if not line.startswith("$"):
                continue
            lines.append(line)
            tag = line[3:6].upper()
            if tag == "RMC":
                have_rmc = True
            elif tag == "GGA":
                have_gga = True
            if have_rmc and have_gga:
                break

        if not lines:
            return None
        return parse_nmea_fix(lines, f"serial:{self.port}")


def maybe_update_time(
    fix: dict[str, object],
    conflict_threshold_sec: float,
    allow_sync: bool,
) -> None:
    if not allow_sync:
        return
    gps_time = fix.get("gps_time")
    if not isinstance(gps_time, datetime.datetime):
        return
    if not system_time_synchronized():
        set_system_time(gps_time)
        return
    drift_sec = abs((current_utc() - gps_time).total_seconds())
    if drift_sec < conflict_threshold_sec:
        return
    # timedatectl's "synchronized" flag only means NTP succeeded at some
    # point this boot, not that it's still accurate -- on this fleet's
    # flaky WireGuard/carrier uplinks it can stay "yes" long after the
    # reference is unreachable while the clock free-runs. A disagreement
    # this large is worth trusting GPS over that stale flag rather than
    # just logging it forever with nothing ever correcting it.
    report_event(
        "gps_time_conflict",
        severity="warning",
        details={
            "system_utc_time": state_value(current_utc()),
            "gps_utc_time": state_value(gps_time),
            "drift_sec": f"{drift_sec:.1f}",
            "source": "gps",
        },
        dedupe_window=1800,
    )
    set_system_time(gps_time)


_server_context: dict[str, str] | None = None
_server_context_loaded = False


def server_context() -> dict[str, str] | None:
    """Best-effort, cached after the first call: server_host/port/peer_uuid/
    api_password if this device is enrolled, else None. A device can run GPS
    entirely locally before ever being enrolled (e.g. bench-testing hardware
    before it's shipped and enrolled), so a missing/incomplete enrollment must
    never be treated as a GPS failure -- it just means location updates stay
    local-only until config-network has run."""
    global _server_context, _server_context_loaded
    if _server_context_loaded:
        return _server_context
    _server_context_loaded = True
    try:
        network_config = read_network_conf()
        server_host = network_config.get("SERVER_WG_IP")
        server_port = network_config.get("SERVER_PORT")
        if not server_host or not server_port:
            return None
        peer_uuid = require_peer_uuid(network_config)
        api_password = read_service_credential("api_password")
    except SystemExit:
        return None
    _server_context = {
        "server_host": server_host,
        "server_port": server_port,
        "peer_uuid": peer_uuid,
        "api_password": api_password,
    }
    return _server_context


def push_location_to_server(latitude: float, longitude: float) -> None:
    ctx = server_context()
    if ctx is None:
        return
    ok, error = send_client_location(
        ctx["server_host"], ctx["server_port"], ctx["peer_uuid"], ctx["api_password"], latitude, longitude
    )
    if ok:
        print(f"Reported location to server: ({latitude:.6f}, {longitude:.6f})")
    else:
        print(f"Could not report location to server (will retry on next GPS-driven update): {error}", file=sys.stderr)


def report_location_change(old_lat: float | None, old_lon: float | None, latitude: float, longitude: float) -> None:
    details = {
        "old_latitude": old_lat,
        "old_longitude": old_lon,
        "latitude": latitude,
        "longitude": longitude,
        "source": "gps",
    }
    if old_lat is not None and old_lon is not None:
        details["distance_m"] = f"{haversine_m(old_lat, old_lon, latitude, longitude):.1f}"
    report_event("gps_location_updated", severity="notice", details=details, dedupe_window=300)
    push_location_to_server(latitude, longitude)


def report_fix_result(fix: tuple[float, float, int] | None) -> None:
    """Reported once per boot, right after the startup fix-acquisition window
    finishes, regardless of whether it changed the recorded location -- so
    there's a record of every boot's attempt, not just the ones that moved
    the stored position."""
    if fix is None:
        report_event("gps_fix_unavailable", severity="notice")
        return
    avg_lat, avg_lon, sample_count = fix
    report_event(
        "gps_fix_acquired",
        severity="info",
        details={"latitude": avg_lat, "longitude": avg_lon, "sample_count": sample_count},
    )


def acquire_initial_fix(read_fix_fn, sample_interval_sec: float, duration_sec: float) -> tuple[float, float, int] | None:
    """Runs once, at service startup only -- not on any ongoing per-cycle
    basis. A device only relocates by being physically unplugged, moved, and
    replugged in, which is a fresh boot either way, so there is no need to
    keep re-deciding location throughout a long uptime; the one-time startup
    sample is the only time it's meaningful to ask "did this device move".

    Samples fixes for up to duration_sec and averages every valid one with a
    plain mean. No outlier-rejection (e.g. median) needed here: the threshold
    this feeds into is kilometers, and ordinary GPS error -- even a single
    bad multipath fix off by a couple hundred meters -- is nowhere near large
    enough to bias a several-sample mean anywhere close to that scale.

    Returns (avg_lat, avg_lon, sample_count), or None if no valid fix was
    obtained in the window at all.
    """
    deadline = time.monotonic() + duration_sec
    samples: list[tuple[float, float]] = []
    while True:
        try:
            fix = read_fix_fn()
        except Exception as exc:
            print(f"Error reading GPS fix during initial acquisition: {exc}", file=sys.stderr)
            fix = None
        if fix is not None:
            latitude = fix.get("latitude")
            longitude = fix.get("longitude")
            if isinstance(latitude, float) and isinstance(longitude, float):
                samples.append((latitude, longitude))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(sample_interval_sec, remaining))

    if not samples:
        return None
    avg_lat = sum(lat for lat, _ in samples) / len(samples)
    avg_lon = sum(lon for _, lon in samples) / len(samples)
    return avg_lat, avg_lon, len(samples)


def maybe_update_location_from_fix(avg_lat: float, avg_lon: float, threshold_m: float) -> None:
    current_lat, current_lon = read_location()
    if current_lat is None or current_lon is None:
        write_location(avg_lat, avg_lon)
        report_location_change(current_lat, current_lon, avg_lat, avg_lon)
        return
    if haversine_m(current_lat, current_lon, avg_lat, avg_lon) >= threshold_m:
        write_location(avg_lat, avg_lon)
        report_location_change(current_lat, current_lon, avg_lat, avg_lon)


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
    serial_port = config_value(config, "GPS_SERIAL_PORT", "")
    serial_baud = config_int(config, "GPS_SERIAL_BAUD", DEFAULT_SERIAL_BAUD)
    allow_sync = config_bool(config, "GPS_SYNC_TIME", True)
    allow_location = config_bool(config, "GPS_UPDATE_LOCATION", True)
    location_threshold_m = max(
        0.0, config_float(config, "GPS_LOCATION_MOVE_THRESHOLD_M", DEFAULT_LOCATION_MOVE_THRESHOLD_M)
    )
    initial_fix_sec = max(0.0, config_float(config, "GPS_INITIAL_FIX_MINUTES", DEFAULT_INITIAL_FIX_MINUTES)) * 60
    conflict_threshold_sec = max(0.0, config_float(config, "GPS_TIME_CONFLICT_SEC", DEFAULT_TIME_CONFLICT_SEC))
    nmea_buffer = ""

    if backend not in ("i2c", "serial"):
        message = f"Unsupported GPS backend '{backend}' (expected 'i2c' or 'serial')"
        print(message, file=sys.stderr)
        write_state("error", message)
        return 1

    serial_gps = SerialGps(serial_port, serial_baud) if backend == "serial" else None

    if backend == "serial":
        source_desc = f"port={serial_port or 'autodetect'} baud={serial_baud}"
    else:
        source_desc = f"i2c_bus={bus_num} i2c_addr={addr_str}"

    print(
        f"sensos-gps starting: backend={backend} {source_desc} interval={interval_sec}s "
        f"sync_time={'yes' if allow_sync else 'no'} "
        f"update_location={'yes' if allow_location else 'no'}"
    )
    write_state(
        "starting",
        f"backend={backend} {source_desc} interval={interval_sec}s "
        f"sync_time={'yes' if allow_sync else 'no'} "
        f"update_location={'yes' if allow_location else 'no'}",
    )

    if allow_location:
        if backend == "i2c":
            def read_one_fix():
                nonlocal nmea_buffer
                fix, nmea_buffer = parse_i2c_gps(bus_num, addr_str, nmea_buffer)
                return fix
        else:
            def read_one_fix():
                assert serial_gps is not None
                return serial_gps.read_fix()

        print(f"Acquiring initial GPS fix (sampling for up to {initial_fix_sec / 60:.1f} min)...")
        initial_fix = acquire_initial_fix(read_one_fix, INITIAL_FIX_SAMPLE_INTERVAL_SEC, initial_fix_sec)
        report_fix_result(initial_fix)
        if initial_fix is not None:
            avg_lat, avg_lon, sample_count = initial_fix
            print(
                f"Initial GPS fix: lat={avg_lat:.6f} lon={avg_lon:.6f} "
                f"(averaged over {sample_count} sample(s))"
            )
            maybe_update_location_from_fix(avg_lat, avg_lon, location_threshold_m)
        else:
            print("No GPS fix acquired during the initial sampling window.", file=sys.stderr)

    while True:
        try:
            if backend == "i2c":
                fix, nmea_buffer = parse_i2c_gps(bus_num, addr_str, nmea_buffer)
            else:
                assert serial_gps is not None
                fix = serial_gps.read_fix()
            if fix is None:
                message = "No valid GPS fix available."
                print(message)
                write_state("no_fix", message)
                time.sleep(interval_sec)
                continue
            message = f"GPS fix: lat={fix['latitude']:.6f} lon={fix['longitude']:.6f} source={fix['source']}"
            print(message)
            maybe_update_time(fix, conflict_threshold_sec, allow_sync)
            if allow_location:
                latitude = fix.get("latitude")
                longitude = fix.get("longitude")
                if isinstance(latitude, float) and isinstance(longitude, float):
                    # A single fix, not an averaged one -- unlike the boot-time
                    # acquisition phase above. Safe on its own here because the
                    # threshold is kilometer-scale (see maybe_update_location_from_fix):
                    # ordinary GPS noise on any one fix is nowhere near enough to
                    # spuriously cross it. This is deliberately cheap and piggybacks
                    # on the regular poll cycle that's already running for time-sync
                    # (which must stay continuous regardless of location -- a box
                    # with no internet/NTP has nothing but GPS to correct clock drift
                    # with) rather than running its own separate polling cadence.
                    maybe_update_location_from_fix(latitude, longitude, location_threshold_m)
            write_state("fix", message, fix)
            time.sleep(interval_sec)
        except Exception as exc:
            message = f"GPS service failure: {exc}"
            print(message, file=sys.stderr)
            write_state("error", message)
            if serial_gps is not None:
                serial_gps.close()
            time.sleep(ERROR_SLEEP_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
