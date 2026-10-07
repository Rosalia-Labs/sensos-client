#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC
#
# Owns the full lifecycle of BirdNET's labeled clips under
# data/audio_recordings/processed/: uploads them to the server, and deletes
# them when local disk space runs low. One process, one loop -- these used to
# be two independent services reading and deleting the same files, which
# meant coordinating state across processes for a race that doesn't exist
# once there's only one reader/writer of this directory and this table.
#
# Deletion is two-tiered:
#   1. The server already has a copy: delete the oldest such clip first.
#      Nothing is lost -- this is the common case whenever the device has
#      connectivity, and it's the only thing that runs on a healthy device.
#   2. Nothing qualifies for (1) -- offline, or uploads haven't caught up:
#      fall back to the original label-diversity thinning (fullest/most
#      redundant label directory first, weakest clip within it), so a
#      long-disconnected device still keeps a representative local archive
#      -- at least one example per label per day -- instead of just filling
#      up with whatever the most common species produces.
#
# sensos-monitor-data-space.sh is the real backstop against a genuinely full
# disk (it stops recording outright below a free-space floor); this service
# doesn't need to duplicate that, only avoid making its job harder.

from __future__ import annotations

import importlib.util
import mimetypes
import os
import shutil
import sys
import time
import traceback
import uuid
from pathlib import Path
from urllib import error, request


SCRIPT_FILE = Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_FILE.parent
OVERLAY_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", "/sensos"))
UTILS_FILE = OVERLAY_ROOT / "libexec" / "utils.py"
UPLOAD_CONFIG_FILE = OVERLAY_ROOT / "etc" / "birdnet-uploads.conf"
AUDIO_ROOT = OVERLAY_ROOT / "data" / "audio_recordings"
OUTPUT_ROOT = AUDIO_ROOT / "processed"
STATE_ROOT = OVERLAY_ROOT / "data" / "birdnet"
sys.path.insert(0, str(SCRIPT_DIR))

from birdnet_data import (
    connect_db,
    ensure_schema,
    mark_audio_sent,
    mark_clip_deleted,
    select_deletable_clip,
    select_pending_audio_uploads,
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


MIN_FREE_PERCENT = float(os.environ.get("BIRDNET_MIN_FREE_PERCENT", "10"))
TARGET_FREE_PERCENT = float(os.environ.get("BIRDNET_TARGET_FREE_PERCENT", "20"))
LOOP_INTERVAL_SEC = int(os.environ.get("BIRDNET_CLIPS_LOOP_INTERVAL_SEC", "60"))
ERROR_SLEEP_SEC = int(os.environ.get("BIRDNET_CLIPS_ERROR_SLEEP_SEC", "30"))
DEFAULT_SESSION_INTERVAL_SEC = 1800


# ---------------------------------------------------------------------------
# Upload phase
# ---------------------------------------------------------------------------


def require_int(config: dict, key: str, *, minimum: int = 1, default: int) -> int:
    raw_value = config.get(key, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise SystemExit(f"[ERROR] Invalid integer for {key}: {raw_value}") from exc
    if value < minimum:
        raise SystemExit(f"[ERROR] {key} must be >= {minimum}.")
    return value


def read_upload_config() -> dict | None:
    """None means "no upload configured on this device" -- this service is
    always-on fleet-wide (it owns disk-space thinning for every device with
    BirdNET audio, not just ones that upload), so a missing
    birdnet-uploads.conf must mean "skip the upload phase", not "crash"."""
    config = read_kv_config(str(UPLOAD_CONFIG_FILE))
    if not config:
        return None
    return {
        "session_interval_sec": require_int(
            config, "SESSION_INTERVAL_SEC", default=DEFAULT_SESSION_INTERVAL_SEC
        ),
        "batch_size": require_int(config, "BATCH_SIZE", default=20),
        "connect_timeout_sec": require_int(config, "CONNECT_TIMEOUT_SEC", default=10),
        "read_timeout_sec": require_int(config, "READ_TIMEOUT_SEC", default=60),
    }


def build_multipart_body(
    fields: dict[str, str], file_field: str, filename: str, file_bytes: bytes
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode(
                "utf-8"
            )
        )
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
        ).encode("utf-8")
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def post_birdnet_audio(
    server_host: str,
    port: str,
    peer_uuid: str,
    api_password: str,
    *,
    channel_index: int,
    clip_start_time: str,
    clip_end_time: str,
    clip_bytes: bytes,
    filename: str,
    connect_timeout_sec: int,
    read_timeout_sec: int,
) -> tuple[int, str]:
    timeout = max(connect_timeout_sec, read_timeout_sec)
    url = f"http://{server_host}:{port}/api/v1/client/peer/birdnet/audio"
    body, content_type = build_multipart_body(
        {
            "channel_index": str(channel_index),
            "clip_start_time": clip_start_time,
            "clip_end_time": clip_end_time,
        },
        "file",
        filename,
        clip_bytes,
    )
    req = request.Request(
        url,
        data=body,
        headers={
            "Content-Type": content_type,
            **build_basic_auth_header(api_password, username=peer_uuid),
        },
        method="PUT",
    )
    with request.urlopen(req, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def drain_pending_uploads(
    config: dict, network_config: dict, api_password: str
) -> None:
    server_host = require_nonempty(network_config.get("SERVER_WG_IP"), "SERVER_WG_IP")
    server_port = require_nonempty(network_config.get("SERVER_PORT"), "SERVER_PORT")
    peer_uuid = require_peer_uuid(network_config)

    while True:
        with connect_db() as conn:
            ensure_schema(conn)
            rows = select_pending_audio_uploads(conn, config["batch_size"])
        if not rows:
            return

        sent = 0
        for row in rows:
            detection_id = int(row["id"])
            clip_path = AUDIO_ROOT / row["clip_path"]
            if not clip_path.is_file():
                # Already gone -- deleted_at will be set for it if thinning
                # was what removed it; either way nothing more to do here,
                # and it naturally stops being selected once that's true.
                print(f"[WARN] Clip file missing, skipping: {clip_path}")
                continue
            try:
                clip_bytes = clip_path.read_bytes()
                status_code, _body = post_birdnet_audio(
                    server_host,
                    server_port,
                    peer_uuid,
                    api_password,
                    channel_index=int(row["channel_index"]),
                    clip_start_time=row["clip_start_time"],
                    clip_end_time=row["clip_end_time"],
                    clip_bytes=clip_bytes,
                    filename=clip_path.name,
                    connect_timeout_sec=config["connect_timeout_sec"],
                    read_timeout_sec=config["read_timeout_sec"],
                )
            except error.HTTPError as exc:
                response_body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 404:
                    print(
                        f"[WARN] No matching detection on server yet for {clip_path} "
                        f"(HTTP 404); will retry next session."
                    )
                else:
                    print(
                        f"[ERROR] BirdNET audio upload failed for {clip_path}: "
                        f"HTTP {exc.code}: {response_body}",
                        file=sys.stderr,
                    )
                continue
            except error.URLError as exc:
                print(
                    f"[ERROR] BirdNET audio upload network error for {clip_path}: {exc}",
                    file=sys.stderr,
                )
                return
            except Exception as exc:
                print(
                    f"[ERROR] BirdNET audio upload failed for {clip_path}: "
                    f"{exc.__class__.__name__}: {exc}",
                    file=sys.stderr,
                )
                continue

            with connect_db() as conn:
                ensure_schema(conn)
                mark_audio_sent(conn, detection_id)
            sent += 1
            print(f"[SUCCESS] Uploaded BirdNET audio clip {clip_path} (HTTP {status_code}).")

        print(f"[INFO] Audio upload pass complete: {sent}/{len(rows)} clip(s) sent.")
        if sent == 0:
            # Nothing in this batch went through (all missing/failed); avoid
            # spinning on the same unsendable batch until the next session.
            return


# ---------------------------------------------------------------------------
# Thinning phase
# ---------------------------------------------------------------------------


def free_percent(path: Path) -> float:
    usage = shutil.disk_usage(path)
    if usage.total <= 0:
        return 0.0
    return (usage.free / usage.total) * 100.0


def choose_redundant_victim(conn) -> tuple[int, Path] | None:
    """Fallback for when nothing is safe to delete for free (tier 1 empty):
    the original label-diversity thinning. Targets the label directory
    holding the most retained audio (the most over-represented / redundant
    species) and removes its weakest-scoring clip, so every label keeps at
    least some representation rather than the archive filling up with
    whichever species is most common."""
    rows = conn.execute(
        """
        SELECT id, clip_path, score, volume, clip_size_bytes
        FROM detections
        WHERE deleted_at IS NULL
          AND clip_path IS NOT NULL
        ORDER BY id
        """
    ).fetchall()
    if not rows:
        return None

    grouped: dict[str, dict] = {}
    updated_size_cache = False
    for row in rows:
        run_id, rel_path = row["id"], row["clip_path"]
        abs_path = AUDIO_ROOT / rel_path
        clip_size_bytes = row["clip_size_bytes"]
        size_bytes = (
            int(clip_size_bytes)
            if clip_size_bytes is not None and int(clip_size_bytes) >= 0
            else None
        )
        if size_bytes is None:
            try:
                size_bytes = int(abs_path.stat().st_size)
                conn.execute(
                    "UPDATE detections SET clip_size_bytes = ? WHERE id = ?",
                    (size_bytes, run_id),
                )
                updated_size_cache = True
            except FileNotFoundError:
                mark_clip_deleted(conn, run_id)
                continue
        label_dir = str(Path(rel_path).parent)
        bucket = grouped.setdefault(
            label_dir, {"clip_count": 0, "total_size_bytes": 0, "rows": []}
        )
        bucket["clip_count"] += 1
        bucket["total_size_bytes"] += size_bytes
        bucket["rows"].append(
            (run_id, rel_path, float(row["score"]), row["volume"], size_bytes)
        )

    if updated_size_cache:
        conn.commit()

    ordered_dirs = sorted(
        grouped.items(),
        key=lambda item: (-item[1]["total_size_bytes"], -item[1]["clip_count"], item[0]),
    )
    for _label_dir, bucket in ordered_dirs:
        candidate_rows = sorted(
            bucket["rows"],
            key=lambda row: (
                row[2],
                1 if row[3] is None else 0,
                float("inf") if row[3] is None else row[3],
                -row[4],
                row[1],
                row[0],
            ),
        )
        for run_id, rel_path, _score, _volume, _size_bytes in candidate_rows:
            abs_path = AUDIO_ROOT / rel_path
            if abs_path.exists():
                return run_id, abs_path
            mark_clip_deleted(conn, run_id)
    return None


def prune_empty_dirs(start: Path) -> None:
    current = start
    while current != OUTPUT_ROOT and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def thin_once(conn) -> bool:
    victim = select_deletable_clip(conn)
    if victim is not None:
        run_id = int(victim["id"])
        abs_path = AUDIO_ROOT / victim["clip_path"]
        if not abs_path.exists():
            mark_clip_deleted(conn, run_id)
            return True
        print(f"Thinning (already uploaded): {abs_path}")
    else:
        fallback = choose_redundant_victim(conn)
        if fallback is None:
            return False
        run_id, abs_path = fallback
        print(f"Thinning (label-diversity fallback, not yet uploaded): {abs_path}")

    abs_path.unlink(missing_ok=True)
    mark_clip_deleted(conn, run_id)
    prune_empty_dirs(abs_path.parent)
    return True


def maybe_thin() -> None:
    current_free = free_percent(AUDIO_ROOT)
    if current_free >= MIN_FREE_PERCENT:
        return
    print(f"[INFO] Free space low: {current_free:.1f}% < {MIN_FREE_PERCENT:.1f}%. Thinning.")
    with connect_db() as conn:
        ensure_schema(conn)
        while current_free < TARGET_FREE_PERCENT:
            if not thin_once(conn):
                print("[WARN] No BirdNET clips available to thin.", file=sys.stderr)
                break
            current_free = free_percent(AUDIO_ROOT)
    print(f"[INFO] Thinning pass complete. Free space now {current_free:.1f}%.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> int:
    setup_logging("manage_birdnet_clips.log")
    # sensos-runner (unprivileged) must never try to repair ownership on a
    # directory another account already created -- ensure_runtime_dir makes
    # the dir if absent and fails loud if it exists but isn't writable,
    # rather than silently chown/chmod'ing it.
    ensure_runtime_dir(STATE_ROOT)
    print(f"manage-birdnet-clips starting: root={AUDIO_ROOT}")

    next_upload_attempt = 0.0
    while True:
        now = time.monotonic()
        if now >= next_upload_attempt:
            config = read_upload_config()
            session_interval_sec = DEFAULT_SESSION_INTERVAL_SEC
            if config is not None:
                session_interval_sec = config["session_interval_sec"]
                try:
                    network_config = read_network_conf()
                    api_password = read_service_credential("api_password")
                    if not network_config:
                        print("[WARN] network.conf missing or empty; skipping upload this pass.")
                    else:
                        drain_pending_uploads(config, network_config, api_password)
                except Exception as exc:
                    print(
                        f"[ERROR] Unhandled BirdNET audio upload error: {exc.__class__.__name__}: {exc}",
                        file=sys.stderr,
                    )
                    traceback.print_exc()
            next_upload_attempt = now + session_interval_sec

        try:
            maybe_thin()
        except Exception as exc:
            print(
                f"[ERROR] Unhandled BirdNET clip thinning error: {exc.__class__.__name__}: {exc}",
                file=sys.stderr,
            )
            traceback.print_exc()
            time.sleep(ERROR_SLEEP_SEC)

        time.sleep(LOOP_INTERVAL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
