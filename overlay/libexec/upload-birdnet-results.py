#!/usr/bin/env python3
# SPDX-License-Identifier: MIT

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sqlite3
import sys
import time
import traceback
from pathlib import Path
from urllib import error, request


SCRIPT_FILE = Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_FILE.parent
OVERLAY_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", "/sensos"))
UTILS_FILE = OVERLAY_ROOT / "libexec" / "utils.py"
CONFIG_FILE = OVERLAY_ROOT / "etc" / "birdnet-uploads.conf"
sys.path.insert(0, str(SCRIPT_DIR))

from birdnet_data import (
    connect_db,
    ensure_schema,
    mark_detections_sent,
    select_pending_detections,
    utcnow_text,
)


if not UTILS_FILE.is_file():
    raise RuntimeError(f"Missing utils.py at {UTILS_FILE}")

UTILS_SPEC = importlib.util.spec_from_file_location("sensos_overlay_utils", UTILS_FILE)
UTILS_MODULE = importlib.util.module_from_spec(UTILS_SPEC)
assert UTILS_SPEC.loader is not None
UTILS_SPEC.loader.exec_module(UTILS_MODULE)

for name in dir(UTILS_MODULE):
    if not name.startswith("_"):
        globals()[name] = getattr(UTILS_MODULE, name)


def require_int(config: dict, key: str, *, minimum: int = 1) -> int:
    raw_value = config.get(key, "").strip()
    if not raw_value:
        raise SystemExit(f"[ERROR] Missing {key} in {CONFIG_FILE}.")
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise SystemExit(f"[ERROR] Invalid integer for {key}: {raw_value}") from exc
    if value < minimum:
        raise SystemExit(f"[ERROR] {key} must be >= {minimum}.")
    return value


def read_upload_config() -> dict:
    config = read_kv_config(str(CONFIG_FILE))
    if not config:
        raise SystemExit(f"[ERROR] Config file missing or empty: {CONFIG_FILE}")
    return {
        "session_interval_sec": require_int(config, "SESSION_INTERVAL_SEC"),
        "batch_size": require_int(config, "BATCH_SIZE"),
        "connect_timeout_sec": require_int(config, "CONNECT_TIMEOUT_SEC"),
        "read_timeout_sec": require_int(config, "READ_TIMEOUT_SEC"),
    }


def detections_to_payload(rows) -> list[dict]:
    return [
        {
            "source_path": row["source_path"],
            "channel_index": int(row["channel_index"]),
            "window_index": int(row["window_index"]),
            "max_score_start_frame": int(row["max_score_start_frame"]),
            "label": row["label"],
            "score": float(row["score"]),
            "likely_score": (
                float(row["likely_score"]) if row["likely_score"] is not None else None
            ),
            "volume": float(row["volume"]) if row["volume"] is not None else None,
            "clip_start_time": row["clip_start_time"],
            "clip_end_time": row["clip_end_time"],
        }
        for row in rows
    ]


def build_upload_payload(hostname: str, client_version: str, detections: list[dict]) -> dict:
    return {
        "schema_version": 2,
        "hostname": hostname,
        "client_version": client_version,
        "sent_at": utcnow_text(),
        "detections": detections,
    }


def parse_upload_response(body: str) -> dict:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"server returned invalid JSON: {exc}") from exc

    status = str(payload.get("status", "")).strip().lower()
    if status != "ok":
        raise ValueError(f"server returned status={status or 'missing'}")

    return {
        "receipt_id": str(payload.get("receipt_id", "")).strip(),
        "accepted_count": payload.get("accepted_count"),
        "server_received_at": str(payload.get("server_received_at", "")).strip(),
    }


def post_birdnet_detections(
    server_host: str,
    port: str,
    peer_uuid: str,
    api_password: str,
    payload: dict,
    *,
    connect_timeout_sec: int,
    read_timeout_sec: int,
) -> tuple[int, str]:
    timeout = max(connect_timeout_sec, read_timeout_sec)
    url = f"http://{server_host}:{port}/api/v1/client/peer/birdnet/batches"
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **build_basic_auth_header(api_password, username=peer_uuid),
        },
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def run_upload_session(config: dict, network_config: dict, api_password: str, client_version: str) -> None:
    server_host = require_nonempty(network_config.get("SERVER_WG_IP"), "SERVER_WG_IP")
    server_port = require_nonempty(network_config.get("SERVER_PORT"), "SERVER_PORT")
    peer_uuid = require_peer_uuid(network_config)
    hostname = socket.gethostname()

    with connect_db() as conn:
        ensure_schema(conn)
        rows = select_pending_detections(conn, config["batch_size"])
    if not rows:
        print("[INFO] No pending BirdNET detections to upload.")
        return

    payload = build_upload_payload(
        hostname=hostname,
        client_version=client_version,
        detections=detections_to_payload(rows),
    )
    response_status = None
    response_body = None
    try:
        print(
            f"[INFO] Uploading {len(rows)} BirdNET detections "
            f"to http://{server_host}:{server_port}/api/v1/client/peer/birdnet/batches"
        )
        response_status, response_body = post_birdnet_detections(
            server_host,
            server_port,
            peer_uuid,
            api_password,
            payload,
            connect_timeout_sec=config["connect_timeout_sec"],
            read_timeout_sec=config["read_timeout_sec"],
        )
        receipt = parse_upload_response(response_body)
    except error.HTTPError as exc:
        response_status = exc.code
        response_body = exc.read().decode("utf-8", errors="replace")
        print(f"[ERROR] BirdNET upload failed: HTTP {exc.code}: {response_body}", file=sys.stderr)
        return
    except error.URLError as exc:
        print(f"[ERROR] BirdNET upload network error: {exc}", file=sys.stderr)
        return
    except Exception as exc:
        message = f"{exc.__class__.__name__}: {exc}"
        if response_status is not None:
            message = f"{message} (HTTP {response_status})"
        if response_body:
            message = f"{message}; response={response_body}"
        print(f"[ERROR] BirdNET upload failed: {message}", file=sys.stderr)
        return

    with connect_db() as conn:
        ensure_schema(conn)
        mark_detections_sent(conn, [int(row["id"]) for row in rows])
    print(
        f"[SUCCESS] Uploaded {len(rows)} BirdNET detections; "
        f"receipt={receipt.get('receipt_id') or 'n/a'} "
        f"accepted={receipt.get('accepted_count')!r} "
        f"server_received_at={receipt.get('server_received_at') or 'n/a'}"
    )


def main() -> int:
    setup_logging("upload_birdnet_results.log")
    config = read_upload_config()
    network_config = read_network_conf()
    api_password = read_api_password()
    if not network_config:
        print("[ERROR] network.conf missing or empty.", file=sys.stderr)
        return 2
    if not api_password:
        print("[ERROR] API password missing.", file=sys.stderr)
        return 2

    client_version = read_client_version_text(str(OVERLAY_ROOT))
    while True:
        try:
            run_upload_session(config, network_config, api_password, client_version)
        except sqlite3.OperationalError as exc:
            print(
                f"[ERROR] BirdNET upload SQLite OperationalError: {exc}",
                file=sys.stderr,
            )
            print(
                "[ERROR] Hint: check birdnet.db permissions, free disk space, "
                "and concurrent DB writers (database locked).",
                file=sys.stderr,
            )
            traceback.print_exc()
        except Exception as exc:
            print(
                f"[ERROR] Unhandled BirdNET upload error: {exc.__class__.__name__}: {exc}",
                file=sys.stderr,
            )
            traceback.print_exc()
        time.sleep(config["session_interval_sec"])


if __name__ == "__main__":
    raise SystemExit(main())
