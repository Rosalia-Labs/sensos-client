# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

"""Shared check functions and persisted state for the pre-deploy check tools.

Each check reports a plain status (what was observed / what was done), not a
pass/fail verdict -- fleet hardware and field situations vary legitimately
(a box may have no TEROS sensor, a site may have no connectivity by design),
so only the operator running the check knows whether a given status is fine
for this box. The tool's job is to make the full state visible and recorded,
not to judge it.

Manual checks (ones that need a human to do something physical, like
listening for noise) persist their result with a timestamp so a later run
can show what was already confirmed instead of silently re-asking.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

CLIENT_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", "/sensos"))
STATE_FILE = CLIENT_ROOT / "etc" / "predeploy-check-state.json"
ARECORD_CONF = CLIENT_ROOT / "etc" / "arecord.conf"
TEROS_CONF = CLIENT_ROOT / "etc" / "teros.conf"
HOTSPOT_CONF = CLIENT_ROOT / "etc" / "hotspot.conf"
WIFI_CONF = CLIENT_ROOT / "etc" / "wifi.conf"
GPSD_DEFAULT_FILE = Path("/etc/default/gpsd")
I2C_DEVICE_NODE = Path("/dev/i2c-1")


def utcnow_text() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def load_state() -> dict:
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(STATE_FILE)


def record_result(state: dict, name: str, status: str, detail: str = "") -> dict:
    entry = {"status": status, "detail": detail, "checked_at": utcnow_text()}
    state[name] = entry
    return entry


def report_line(name: str, entry: dict) -> str:
    status = entry.get("status", "unknown")
    detail = entry.get("detail", "")
    checked_at = entry.get("checked_at", "")
    suffix = f" -- {detail}" if detail else ""
    return f"[{status}] {name}{suffix} ({checked_at})"


def stdin_is_interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def confirm(prompt: str, default: bool = False) -> bool | None:
    """Ask a yes/no question. Returns None if stdin isn't interactive."""
    if not stdin_is_interactive():
        return None
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{prompt} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def ask_text(prompt: str) -> str:
    if not stdin_is_interactive():
        return ""
    return input(f"{prompt}: ").strip()


def read_kv_config(path: Path) -> dict:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        values[key.strip()] = val
    return values


def already_done(state: dict, name: str) -> dict | None:
    entry = state.get(name)
    if entry and entry.get("status") not in (None, "not_verified"):
        return entry
    return None


def maybe_skip_manual_check(state: dict, name: str) -> dict | None:
    """If this manual check has a prior result, ask whether to redo it.

    Returns the prior entry if the operator chooses to keep it (or if stdin
    isn't interactive, in which case the prior result is kept automatically
    and nothing is asked), or None if the check should run again.
    """
    prior = already_done(state, name)
    if prior is None:
        return None
    redo = confirm(
        f"'{name}' was already checked on {prior['checked_at']} "
        f"({prior['status']}: {prior.get('detail', '')}). Redo it?",
        default=False,
    )
    if redo is None or redo is False:
        return prior
    return None


def check_audio_hardware_present() -> tuple[str, str]:
    """Probe each configured recording device with a cheap hw-params dump."""
    config = read_kv_config(ARECORD_CONF)
    device = config.get("DEVICE")
    fmt = config.get("FORMAT", "S16_LE")
    if not device:
        return "not_detected", f"no DEVICE set in {ARECORD_CONF}"

    try:
        result = subprocess.run(
            ["arecord", "--dump-hw-params", "-D", device, "-d", "1", "-f", fmt, "/dev/null"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return "not_detected", "arecord not found on this system"
    if result.returncode == 0:
        return "detected", f"device {device} responded"
    return "not_detected", f"device {device} did not respond: {result.stderr.strip()[:200]}"


def check_audio_listen(state: dict) -> tuple[str, str]:
    """Manual check: play a short recording back over SSH so a human can
    listen for abnormal noise (e.g. the periodic 'put-put-put' interference
    found on one field unit). There's no speaker on the box itself, so this
    streams audio to whatever machine the operator is already SSH'd in
    from (the same laptop link used for on-site configuration) rather than
    requiring any new hardware on the client.
    """
    name = "audio_listen"
    prior = maybe_skip_manual_check(state, name)
    if prior is not None:
        return prior["status"], prior["detail"]

    config = read_kv_config(ARECORD_CONF)
    device = config.get("DEVICE", "<DEVICE from arecord.conf>")
    fmt = config.get("FORMAT", "S16_LE")
    channels = config.get("CHANNELS", "1")
    rate = config.get("RATE", "48000")

    print()
    print("Manual check: listen for abnormal noise (e.g. clicking, buzzing, periodic artifacts).")
    print("Run this from your laptop, substituting your actual SSH target, and use")
    print("headphones (a laptop speaker in the room can mask the exact noise and lab")
    print("background noise can cause false negatives):")
    print()
    print(
        f'  ssh <user>@<pi-host> "arecord -D {device} -f {fmt} -c {channels} -r {rate} -d 10" '
        f"| aplay -f {fmt} -c {channels} -r {rate} -"
    )
    print()

    listened = confirm("Press Enter once ready -- have you listened?", default=True)
    if listened is None:
        return "not_verified", "skipped: not an interactive session"

    abnormal = confirm("Did you hear anything abnormal?", default=False)
    if abnormal:
        note = ask_text("Briefly describe what you heard")
        return "done", f"abnormal noise reported: {note}" if note else "abnormal noise reported"
    return "done", "no abnormal noise heard"


def check_i2c_bus() -> tuple[str, str]:
    """Report which addresses respond on the I2C bus, same technique
    i2cdetect/debug-i2c/read-i2c-sensors.py's startup scan use. This doesn't
    compare against which sensors are expected -- that's fleet/box-specific
    and left for the operator to judge against the raw addresses found.
    """
    if not I2C_DEVICE_NODE.exists():
        return "not_enabled", f"{I2C_DEVICE_NODE} missing; Linux I2C is not enabled"

    try:
        result = subprocess.run(
            ["i2cdetect", "-y", "1"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return "not_detected", "i2cdetect not found on this system"

    found_set: set[str] = set()
    for line in result.stdout.lower().splitlines():
        header, sep, rest = line.partition(":")
        if not sep or not re.fullmatch(r"[0-9a-f]0", header.strip()):
            continue
        row_base = int(header.strip(), 16)
        for col, cell in enumerate(rest.split()):
            if cell == "uu":
                # Address claimed by a kernel driver (can't be probed), but
                # that still means hardware is present at this address.
                found_set.add(f"{row_base + col:02x}")
            elif re.fullmatch(r"[0-9a-f]{2}", cell):
                found_set.add(cell)
    found = sorted(found_set)
    if not found:
        return "no_addresses", "I2C bus enabled, no devices responded"
    return "detected", "addresses: " + ", ".join(f"0x{addr}" for addr in found)


def check_teros_serial() -> tuple[str, str]:
    """Report whether the TEROS USB node configured in teros.conf is
    currently present at its resolved device path."""
    config = read_kv_config(TEROS_CONF)
    device = config.get("TEROS_DEVICE")
    if not device:
        return "not_configured", f"no TEROS_DEVICE in {TEROS_CONF} -- not configured on this unit"
    if os.path.exists(device):
        return "detected", f"configured device {device} is present"
    return "not_detected", f"configured device {device} not present (unplugged?)"


def check_gps_fix(collect_seconds: float = 5.0) -> tuple[str, str]:
    """One-shot query of gpsd's own JSON protocol for a current fix. Reads
    through gpsd (127.0.0.1:2947) rather than the serial device directly --
    gpsd is the sole owner of that device (chrony also reads it via gpsd's
    SHM feed for time), so a second direct reader would conflict with gpsd
    the same way it would conflict with the real sensos-gps.py service.
    """
    gpsd_config = read_kv_config(GPSD_DEFAULT_FILE)
    configured_device = gpsd_config.get("DEVICES", "")
    if not configured_device:
        return "not_configured", f"gpsd has no DEVICES= in {GPSD_DEFAULT_FILE} (run config-gps --backend serial)"
    if not os.path.exists(configured_device):
        return "not_detected", f"configured device {configured_device} not present (unplugged?)"

    try:
        sock = socket.create_connection(("127.0.0.1", 2947), timeout=5)
    except OSError as exc:
        return "gpsd_not_running", f"could not connect to gpsd: {exc}"

    try:
        sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
        buffer = ""
        deadline = time.monotonic() + collect_seconds
        tpv = None
        while time.monotonic() < deadline and tpv is None:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buffer += chunk.decode("ascii", errors="ignore")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue
                try:
                    report = json.loads(line)
                except ValueError:
                    continue
                if report.get("class") == "TPV":
                    tpv = report
                    break
    finally:
        sock.close()

    if tpv is None:
        return "no_fix", f"connected to gpsd on {configured_device}, no fix yet"
    mode = tpv.get("mode", 0)
    if mode >= 2:
        return "fix_acquired", f"lat={tpv.get('lat')}, lon={tpv.get('lon')}"
    return "no_fix", f"connected to gpsd on {configured_device}, mode={mode}"


def check_uplink_intent() -> tuple[str, str]:
    """Report the uplink Wi-Fi intent file written by config-wifi, if any."""
    config = read_kv_config(WIFI_CONF)
    ssid = config.get("UPLINK_SSID")
    if not ssid:
        return "not_configured", f"no uplink configured ({WIFI_CONF} missing or empty)"
    iface = config.get("UPLINK_INTERFACE", "unknown")
    return "configured", f"ssid={ssid!r}, interface={iface}"


def check_hotspot_intent() -> tuple[str, str]:
    """Report the local-AP intent file written by config-hotspot, if any."""
    config = read_kv_config(HOTSPOT_CONF)
    ssid = config.get("AP_SSID")
    if not ssid:
        return "not_configured", f"no local AP configured ({HOTSPOT_CONF} missing or empty)"
    enabled = config.get("AP_ENABLED", "unknown")
    iface = config.get("AP_INTERFACE", "unknown")
    return "configured", f"ssid={ssid!r}, enabled={enabled}, interface={iface}"
