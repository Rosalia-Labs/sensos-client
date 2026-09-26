#!/usr/bin/env python3
# SPDX-License-Identifier: MIT

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib import error, request
from urllib.parse import quote


SCRIPT_FILE = Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_FILE.parent
OVERLAY_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", "/sensos"))
UTILS_FILE = OVERLAY_ROOT / "libexec" / "utils.py"
CONFIG_FILE = OVERLAY_ROOT / "etc" / "birdweather-uploads.conf"
LOCATION_CONF = OVERLAY_ROOT / "etc" / "location.conf"
sys.path.insert(0, str(SCRIPT_DIR))

from birdnet_data import connect_db, ensure_schema
from birdweather_data import (
    ensure_birdweather_schema,
    mark_birdweather_sent,
    mark_low_score_birdweather_detections_skipped,
    save_birdweather_soundscape_id,
    select_pending_birdweather_detections,
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

BIRDWEATHER_API_ROOT = "https://app.birdweather.com/api/v1"


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


def read_upload_threshold(config: dict, key: str) -> float:
    """Optional upload-side filter threshold; missing = 0.0 = no filtering.
    See birdnet_data.read_upload_threshold's twin in upload-birdnet-results.py
    -- same reasoning applies here, independently."""
    raw_value = config.get(key, "").strip()
    if not raw_value:
        return 0.0
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise SystemExit(f"[ERROR] Invalid float for {key}: {raw_value}") from exc
    if not 0.0 <= value <= 1.0:
        raise SystemExit(f"[ERROR] {key} must be between 0 and 1, got {raw_value}.")
    return value


def read_bool(config: dict, key: str, default: bool) -> bool:
    raw_value = config.get(key, "").strip().lower()
    if not raw_value:
        return default
    return raw_value not in ("false", "0", "no", "off")


def read_upload_config() -> dict:
    config = read_kv_config(str(CONFIG_FILE))
    if not config:
        raise SystemExit(f"[ERROR] Config file missing or empty: {CONFIG_FILE}")
    return {
        "session_interval_sec": require_int(config, "SESSION_INTERVAL_SEC"),
        "connect_timeout_sec": require_int(config, "CONNECT_TIMEOUT_SEC"),
        "read_timeout_sec": require_int(config, "READ_TIMEOUT_SEC"),
        "upload_audio": read_bool(config, "UPLOAD_AUDIO", False),
        "upload_min_score": read_upload_threshold(config, "UPLOAD_MIN_SCORE"),
        "upload_min_likelihood": read_upload_threshold(config, "UPLOAD_MIN_LIKELIHOOD"),
        "upload_min_volume": read_upload_threshold(config, "UPLOAD_MIN_VOLUME"),
        "upload_min_score_x_likelihood": read_upload_threshold(
            config, "UPLOAD_MIN_SCORE_X_LIKELIHOOD"
        ),
    }


def read_station_location() -> tuple[float, float] | None:
    config = read_kv_config(str(LOCATION_CONF))
    try:
        return float(config["LATITUDE"]), float(config["LONGITUDE"])
    except (KeyError, TypeError, ValueError):
        return None


def split_label(label: str) -> tuple[str, str]:
    """BirdNET labels are 'Scientific name_Common Name'. BirdWeather requires
    both separately; fall back to using the whole label as the common name
    if a label ever doesn't contain the separator, rather than crashing the
    uploader over one malformed row."""
    if "_" in label:
        scientific_name, common_name = label.split("_", 1)
        if scientific_name and common_name:
            return scientific_name, common_name
    print(f"[WARN] Could not split BirdNET label into scientific/common name: {label!r}", file=sys.stderr)
    return "", label


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def clip_duration_seconds(row) -> float | None:
    try:
        start = parse_iso(row["clip_start_time"])
        end = parse_iso(row["clip_end_time"])
    except Exception:
        return None
    duration = (end - start).total_seconds()
    return duration if duration > 0 else None


def upload_soundscape(
    station_token: str, clip_path: str, timestamp: str, *, connect_timeout_sec: int, read_timeout_sec: int
) -> int:
    """POSTs the raw audio file; returns the server-assigned soundscape id."""
    with open(clip_path, "rb") as handle:
        audio_bytes = handle.read()
    url = (
        f"{BIRDWEATHER_API_ROOT}/stations/{station_token}/soundscapes"
        f"?timestamp={quote(timestamp)}"
    )
    req = request.Request(
        url,
        data=audio_bytes,
        headers={"Content-Type": "audio/flac"},
        method="POST",
    )
    timeout = max(connect_timeout_sec, read_timeout_sec)
    with request.urlopen(req, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8", errors="replace"))
    soundscape_id = body.get("soundscape", {}).get("id")
    if soundscape_id is None:
        raise ValueError(f"BirdWeather soundscape upload response missing id: {body!r}")
    return int(soundscape_id)


def post_detection(
    station_token: str, payload: dict, *, connect_timeout_sec: int, read_timeout_sec: int
) -> None:
    url = f"{BIRDWEATHER_API_ROOT}/stations/{station_token}/detections"
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    timeout = max(connect_timeout_sec, read_timeout_sec)
    with request.urlopen(req, timeout=timeout) as response:
        response.read()


def upload_one_detection(
    conn, row, station_token: str, config: dict, location: tuple[float, float] | None
) -> None:
    detection_id = int(row["id"])
    scientific_name, common_name = split_label(row["weighted_label"] or row["label"])
    timestamp = row["clip_start_time"]

    payload = {
        "timestamp": timestamp,
        "commonName": common_name,
        "scientificName": scientific_name,
        "confidence": float(row["weighted_score"]),
    }
    if location is not None:
        payload["lat"], payload["lon"] = location

    soundscape_id = row["birdweather_soundscape_id"]
    clip_path = row["clip_path"]
    if config["upload_audio"] and soundscape_id is None and clip_path and os.path.isfile(clip_path):
        try:
            soundscape_id = upload_soundscape(
                station_token,
                clip_path,
                timestamp,
                connect_timeout_sec=config["connect_timeout_sec"],
                read_timeout_sec=config["read_timeout_sec"],
            )
            save_birdweather_soundscape_id(conn, detection_id, soundscape_id)
            print(f"[INFO] Uploaded soundscape for detection {detection_id} (id={soundscape_id}).")
        except Exception as exc:
            # Soundscape upload is a nice-to-have; a failure here must not
            # block the detection metadata itself from going up.
            print(f"[WARN] Soundscape upload failed for detection {detection_id}: {exc}", file=sys.stderr)
            soundscape_id = None
    elif config["upload_audio"] and clip_path and not os.path.isfile(clip_path):
        print(
            f"[INFO] Clip for detection {detection_id} no longer on disk "
            "(likely thinned); uploading metadata only.",
        )

    if soundscape_id is not None:
        payload["soundscapeId"] = int(soundscape_id)
        duration = clip_duration_seconds(row)
        if duration is not None:
            payload["soundscapeStartTime"] = 0.0
            payload["soundscapeEndTime"] = duration

    post_detection(
        station_token,
        payload,
        connect_timeout_sec=config["connect_timeout_sec"],
        read_timeout_sec=config["read_timeout_sec"],
    )
    mark_birdweather_sent(conn, detection_id)
    print(f"[SUCCESS] Uploaded detection {detection_id} ({common_name}) to BirdWeather.")


def run_upload_session(station_token: str, config: dict, location: tuple[float, float] | None) -> bool:
    """Returns True when the caller should sleep before the next attempt,
    False when more pending detections remain and the caller should
    immediately run another session to drain backlog -- same contract as
    upload-birdnet-results.py's run_upload_session()."""
    with connect_db() as conn:
        ensure_schema(conn)
        ensure_birdweather_schema(conn)
        skipped = mark_low_score_birdweather_detections_skipped(
            conn,
            config["upload_min_score"],
            config["upload_min_likelihood"],
            config["upload_min_volume"],
            config["upload_min_score_x_likelihood"],
        )
        if skipped:
            print(f"[INFO] Skipped {skipped} low-score detection(s) below BirdWeather upload thresholds.")
        rows = select_pending_birdweather_detections(conn, 1)
    if not rows:
        print("[INFO] No pending BirdNET detections to upload to BirdWeather.")
        return True

    row = rows[0]
    try:
        with connect_db() as conn:
            ensure_schema(conn)
            ensure_birdweather_schema(conn)
            upload_one_detection(conn, row, station_token, config, location)
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"[ERROR] BirdWeather upload failed: HTTP {exc.code}: {body}", file=sys.stderr)
        return True
    except error.URLError as exc:
        print(f"[ERROR] BirdWeather upload network error: {exc}", file=sys.stderr)
        return True
    except Exception as exc:
        print(f"[ERROR] BirdWeather upload failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return True

    with connect_db() as conn:
        ensure_schema(conn)
        ensure_birdweather_schema(conn)
        has_more = bool(select_pending_birdweather_detections(conn, 1))
    if has_more:
        print("[INFO] Additional pending BirdWeather detections remain; continuing without sleep.")
    return not has_more


def main() -> int:
    setup_logging("upload_birdweather_results.log")
    config = read_upload_config()
    station_token = read_service_credential("birdweather_token")
    if not station_token:
        print("[ERROR] Empty BirdWeather station token.", file=sys.stderr)
        return 2
    location = read_station_location()
    if location is None:
        print(
            "[INFO] No location.conf LATITUDE/LONGITUDE set; detections will use "
            "this station's own location as configured on BirdWeather's side."
        )
    while True:
        should_sleep = True
        try:
            should_sleep = run_upload_session(station_token, config, location)
        except sqlite3.OperationalError as exc:
            print(f"[ERROR] BirdWeather upload SQLite OperationalError: {exc}", file=sys.stderr)
            traceback.print_exc()
        except Exception as exc:
            print(f"[ERROR] Unhandled BirdWeather upload error: {exc.__class__.__name__}: {exc}", file=sys.stderr)
            traceback.print_exc()
        if should_sleep:
            time.sleep(config["session_interval_sec"])


if __name__ == "__main__":
    raise SystemExit(main())
