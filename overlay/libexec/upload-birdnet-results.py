#!/usr/bin/env python3
# SPDX-License-Identifier: MIT

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import time
from pathlib import Path
from urllib import error, request


SCRIPT_FILE = Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_FILE.parent
OVERLAY_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", "/sensos"))
UTILS_FILE = OVERLAY_ROOT / "libexec" / "utils.py"
CONFIG_FILE = OVERLAY_ROOT / "etc" / "birdnet-uploads.conf"
sys.path.insert(0, str(SCRIPT_DIR))

from birdnet_data import (
    MODE_CLIENT_RETAINS,
    MODE_SERVER_OWNS,
    connect_db,
    create_upload_batch,
    ensure_schema,
    fetch_detections_for_sources,
    fetch_flac_runs_for_sources,
    mark_upload_failure,
    mark_upload_success,
    prune_server_owned_results,
    select_pending_processed_files,
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


def optional_int(config: dict, key: str) -> int | None:
    raw_value = config.get(key, "").strip()
    if not raw_value:
        return None
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise SystemExit(f"[ERROR] Invalid integer for {key}: {raw_value}") from exc
    if value <= 0:
        raise SystemExit(f"[ERROR] {key} must be > 0 when set.")
    return value


def read_birdnet_upload_config() -> dict:
    config = read_kv_config(str(CONFIG_FILE))
    if not config:
        raise SystemExit(f"[ERROR] Config file missing or empty: {CONFIG_FILE}")

    ownership_mode = config.get("OWNERSHIP_MODEL", "").strip()
    if ownership_mode not in (MODE_CLIENT_RETAINS, MODE_SERVER_OWNS):
        raise SystemExit(
            f"[ERROR] OWNERSHIP_MODEL must be '{MODE_CLIENT_RETAINS}' or '{MODE_SERVER_OWNS}'."
        )

    delete_after_days = optional_int(config, "DELETE_AFTER_DAYS")
    if ownership_mode != MODE_SERVER_OWNS and delete_after_days is not None:
        raise SystemExit(
            "[ERROR] DELETE_AFTER_DAYS may only be set when OWNERSHIP_MODEL=server-owns."
        )

    return {
        "ownership_mode": ownership_mode,
        "session_interval_sec": require_int(config, "SESSION_INTERVAL_SEC"),
        "batch_size": require_int(config, "BATCH_SIZE"),
        "connect_timeout_sec": require_int(config, "CONNECT_TIMEOUT_SEC"),
        "read_timeout_sec": require_int(config, "READ_TIMEOUT_SEC"),
        "delete_after_days": delete_after_days,
    }


def build_birdnet_upload_payload(
    *,
    hostname: str,
    client_version: str,
    batch_id: int,
    ownership_mode: str,
    processed_files: list[dict],
) -> dict:
    source_paths = [entry["source_path"] for entry in processed_files]
    processed_times = [entry["processed_at"] for entry in processed_files]
    return {
        "schema_version": 1,
        "hostname": hostname,
        "client_version": client_version,
        "batch_id": batch_id,
        "sent_at": utcnow_text(),
        "ownership_mode": ownership_mode,
        "source_count": len(processed_files),
        "first_source_path": min(source_paths),
        "last_source_path": max(source_paths),
        "first_processed_at": min(processed_times),
        "last_processed_at": max(processed_times),
        "processed_files": processed_files,
    }


def parse_upload_response(body: str, expected_count: int) -> dict:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"server returned invalid JSON: {exc}") from exc

    receipt_id = str(payload.get("receipt_id", "")).strip()
    accepted_count = payload.get("accepted_count")
    server_received_at = str(payload.get("server_received_at", "")).strip()
    status = str(payload.get("status", "")).strip().lower()

    if status != "ok":
        raise ValueError(f"server returned status={status or 'missing'}")
    if not receipt_id:
        raise ValueError("server response missing receipt_id")
    if accepted_count != expected_count:
        raise ValueError(
            f"server accepted_count {accepted_count!r} did not match {expected_count}"
        )
    if not server_received_at:
        raise ValueError("server response missing server_received_at")

    return {
        "receipt_id": receipt_id,
        "accepted_count": accepted_count,
        "server_received_at": server_received_at,
    }


def post_birdnet_batch(
    server_ip: str,
    port: str,
    peer_uuid: str,
    api_password: str,
    payload: dict,
    *,
    connect_timeout_sec: int,
    read_timeout_sec: int,
) -> tuple[int, str]:
    timeout = max(connect_timeout_sec, read_timeout_sec)
    url = f"http://{server_ip}:{port}/api/v1/client/peer/birdnet/batches"
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


def source_rows_to_payload(conn, rows) -> list[dict]:
    source_paths = [str(row["source_path"]) for row in rows]
    detections_by_source = fetch_detections_for_sources(conn, source_paths)
    flac_runs_by_source = fetch_flac_runs_for_sources(conn, source_paths)
    payload_sources = []
    for row in rows:
        source_path = str(row["source_path"])
        payload_sources.append(
            {
                "source_path": source_path,
                "sample_rate": int(row["sample_rate"]),
                "channels": int(row["channels"]),
                "frames": int(row["frames"]),
                "started_at": str(row["started_at"]),
                "processed_at": str(row["processed_at"]),
                "status": str(row["status"]),
                "error": row["error"],
                "output_dir": row["output_dir"],
                "deleted_source": bool(row["deleted_source"]),
                "detections": [
                    {
                        "channel_index": int(det["channel_index"]),
                        "window_index": int(det["window_index"]),
                        "start_frame": int(det["start_frame"]),
                        "end_frame": int(det["end_frame"]),
                        "start_sec": float(det["start_sec"]),
                        "end_sec": float(det["end_sec"]),
                        "top_label": str(det["top_label"]),
                        "top_score": float(det["top_score"]),
                        "top_likely_score": (
                            None
                            if det["top_likely_score"] is None
                            else float(det["top_likely_score"])
                        ),
                    }
                    for det in detections_by_source.get(source_path, [])
                ],
                "flac_runs": [
                    {
                        "channel_index": int(run["channel_index"]),
                        "run_index": int(run["run_index"]),
                        "label": str(run["label"]),
                        "label_dir": run["label_dir"],
                        "start_frame": int(run["start_frame"]),
                        "end_frame": int(run["end_frame"]),
                        "start_sec": float(run["start_sec"]),
                        "end_sec": float(run["end_sec"]),
                        "peak_score": float(run["peak_score"]),
                        "peak_likely_score": (
                            None
                            if run["peak_likely_score"] is None
                            else float(run["peak_likely_score"])
                        ),
                        "flac_path": str(run["flac_path"]),
                        "deleted_at": run["deleted_at"],
                    }
                    for run in flac_runs_by_source.get(source_path, [])
                ],
            }
        )
    return payload_sources


def run_upload_session(config: dict, network_config: dict, api_password: str, client_version: str) -> None:
    server_ip = require_nonempty(network_config.get("SERVER_WG_IP"), "SERVER_WG_IP")
    server_port = require_nonempty(network_config.get("SERVER_PORT"), "SERVER_PORT")
    peer_uuid = require_peer_uuid(network_config)
    hostname = socket.gethostname()

    with connect_db() as conn:
        ensure_schema(conn)
        rows = select_pending_processed_files(conn, config["batch_size"])
        if not rows:
            if config["ownership_mode"] == MODE_SERVER_OWNS and config["delete_after_days"] is not None:
                deleted_sources, deleted_flacs = prune_server_owned_results(
                    conn,
                    older_than_days=config["delete_after_days"],
                )
                if deleted_sources:
                    print(
                        f"[INFO] Pruned {deleted_sources} server-owned BirdNET source rows "
                        f"and {deleted_flacs} local FLAC clips."
                    )
            print("[INFO] No pending BirdNET results to upload.")
            return

        batch_id = create_upload_batch(conn, rows, config["ownership_mode"])
        payload_sources = source_rows_to_payload(conn, rows)

    payload = build_birdnet_upload_payload(
        hostname=hostname,
        client_version=client_version,
        batch_id=batch_id,
        ownership_mode=config["ownership_mode"],
        processed_files=payload_sources,
    )

    response_status = None
    response_body = None
    try:
        print(
            f"[INFO] Uploading BirdNET batch {batch_id} with {len(payload_sources)} processed files "
            f"to http://{server_ip}:{server_port}/api/v1/client/peer/birdnet/batches"
        )
        response_status, response_body = post_birdnet_batch(
            server_ip,
            server_port,
            peer_uuid,
            api_password,
            payload,
            connect_timeout_sec=config["connect_timeout_sec"],
            read_timeout_sec=config["read_timeout_sec"],
        )
        receipt = parse_upload_response(response_body, len(payload_sources))
    except error.HTTPError as exc:
        response_status = exc.code
        response_body = exc.read().decode("utf-8", errors="replace")
        message = f"HTTP {exc.code}: {response_body}"
        with connect_db() as conn:
            ensure_schema(conn)
            mark_upload_failure(conn, batch_id, rows, message, response_status, response_body)
        print(f"[ERROR] BirdNET upload failed for batch {batch_id}: {message}", file=sys.stderr)
        return
    except Exception as exc:
        message = str(exc)
        with connect_db() as conn:
            ensure_schema(conn)
            mark_upload_failure(conn, batch_id, rows, message, response_status, response_body)
        print(f"[ERROR] BirdNET upload failed for batch {batch_id}: {message}", file=sys.stderr)
        return

    with connect_db() as conn:
        ensure_schema(conn)
        mark_upload_success(
            conn,
            batch_id,
            rows,
            config["ownership_mode"],
            receipt["receipt_id"],
            receipt["server_received_at"],
            response_status,
            response_body,
        )
        if config["ownership_mode"] == MODE_SERVER_OWNS and config["delete_after_days"] is not None:
            deleted_sources, deleted_flacs = prune_server_owned_results(
                conn,
                older_than_days=config["delete_after_days"],
            )
            if deleted_sources:
                print(
                    f"[INFO] Pruned {deleted_sources} server-owned BirdNET source rows "
                    f"and {deleted_flacs} local FLAC clips."
                )

    print(
        f"[SUCCESS] Uploaded BirdNET batch {batch_id}; receipt={receipt['receipt_id']} "
        f"accepted={receipt['accepted_count']}"
    )


def main() -> None:
    setup_logging("upload_birdnet_results.log")
    config = read_birdnet_upload_config()
    network_config = read_network_conf()
    api_password = read_api_password()
    client_version = read_client_version_text(str(OVERLAY_ROOT))

    print(
        "[INFO] Starting BirdNET upload loop "
        f"(ownership_mode={config['ownership_mode']}, session_interval_sec={config['session_interval_sec']})"
    )
    while True:
        try:
            run_upload_session(config, network_config, api_password, client_version)
        except Exception as exc:
            print(f"[ERROR] BirdNET upload session failed: {exc}", file=sys.stderr)
        time.sleep(config["session_interval_sec"])


if __name__ == "__main__":
    main()
