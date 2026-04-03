#!/usr/bin/env python3
# SPDX-License-Identifier: MIT

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import sys
from pathlib import Path
from urllib import error, request


SCRIPT_FILE = Path(__file__).resolve()
OVERLAY_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", "/sensos"))
UTILS_FILE = OVERLAY_ROOT / "libexec" / "utils.py"
CONFIG_FILE = OVERLAY_ROOT / "etc" / "network.conf"
API_PASS_FILE = OVERLAY_ROOT / "keys" / "api_password"
VERSION_FILE = OVERLAY_ROOT / "VERSION"

if not UTILS_FILE.is_file():
    raise RuntimeError(f"Missing utils.py at {UTILS_FILE}")

UTILS_SPEC = importlib.util.spec_from_file_location("sensos_overlay_utils", UTILS_FILE)
UTILS_MODULE = importlib.util.module_from_spec(UTILS_SPEC)
assert UTILS_SPEC.loader is not None
UTILS_SPEC.loader.exec_module(UTILS_MODULE)

for name in dir(UTILS_MODULE):
    if not name.startswith("_"):
        globals()[name] = getattr(UTILS_MODULE, name)


def read_required_text(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"[ERROR] {path} not found.")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise SystemExit(f"[ERROR] {path} is empty.")
    return value


def read_uptime_seconds() -> int:
    with open("/proc/uptime", "r", encoding="utf-8") as handle:
        return int(float(handle.read().split()[0]))


def read_memory_totals_mb() -> tuple[int, int]:
    mem_total_kb = 0
    mem_available_kb = 0
    with open("/proc/meminfo", "r", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("MemTotal:"):
                mem_total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_available_kb = int(line.split()[1])
    mem_total_mb = mem_total_kb // 1024
    mem_used_mb = (mem_total_kb - mem_available_kb) // 1024
    return mem_used_mb, mem_total_mb


def read_load_averages() -> tuple[float, float, float]:
    load_1m, load_5m, load_15m = os.getloadavg()
    return float(load_1m), float(load_5m), float(load_15m)


def build_client_status_payload(
    *,
    wireguard_ip: str,
    version: str,
    hostname: str,
    uptime_seconds: int,
    disk_available_gb: int,
    memory_used_mb: int,
    memory_total_mb: int,
    load_1m: float,
    load_5m: float,
    load_15m: float,
    status_message: str = "OK",
) -> dict:
    return {
        "wireguard_ip": wireguard_ip,
        "hostname": hostname,
        "uptime_seconds": uptime_seconds,
        "disk_available_gb": disk_available_gb,
        "memory_used_mb": memory_used_mb,
        "memory_total_mb": memory_total_mb,
        "load_1m": load_1m,
        "load_5m": load_5m,
        "load_15m": load_15m,
        "version": version,
        "status_message": status_message,
    }


def collect_client_status_payload(config: dict, version: str) -> dict:
    server_wg_ip = config.get("SERVER_WG_IP")
    server_port = config.get("SERVER_PORT")
    wireguard_ip = config.get("CLIENT_WG_IP")

    if not server_wg_ip or not server_port or not wireguard_ip:
        raise SystemExit(
            f"[ERROR] SERVER_WG_IP, SERVER_PORT, or CLIENT_WG_IP missing in {CONFIG_FILE}."
        )

    disk_available_gb = shutil.disk_usage("/").free // (1024 ** 3)
    memory_used_mb, memory_total_mb = read_memory_totals_mb()
    load_1m, load_5m, load_15m = read_load_averages()

    return build_client_status_payload(
        wireguard_ip=wireguard_ip,
        hostname=socket.gethostname(),
        uptime_seconds=read_uptime_seconds(),
        disk_available_gb=disk_available_gb,
        memory_used_mb=memory_used_mb,
        memory_total_mb=memory_total_mb,
        load_1m=load_1m,
        load_5m=load_5m,
        load_15m=load_15m,
        version=version,
    )


def post_status_update(server_ip: str, port: str, api_password: str, payload: dict, timeout: int = 10):
    api_url = f"http://{server_ip}:{port}/client-status"
    req = request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **build_basic_auth_header(api_password),
        },
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as response:
        response.read()


def main() -> int:
    version = read_required_text(VERSION_FILE)
    config = read_network_conf()
    api_password = read_required_text(API_PASS_FILE)
    payload = collect_client_status_payload(config, version)
    server_ip = config["SERVER_WG_IP"]
    server_port = config["SERVER_PORT"]

    print(f"[INFO] Sending status to http://{server_ip}:{server_port}/client-status")
    try:
        post_status_update(server_ip, server_port, api_password, payload)
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"[ERROR] Status post failed: HTTP {exc.code}: {body}") from exc
    except Exception as exc:
        raise SystemExit(f"[ERROR] Status post failed: {exc}") from exc

    print("[SUCCESS] Status posted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
