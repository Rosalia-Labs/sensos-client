#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import argparse
import importlib.util
import os
import shutil
import sqlite3
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

setup_logging = UTILS_MODULE.setup_logging
create_dir = UTILS_MODULE.create_dir

DATA_ROOT = CLIENT_ROOT / "data"
AUDIO_ROOT = DATA_ROOT / "audio_recordings"
OUTPUT_ROOT = AUDIO_ROOT / "processed"
STATE_ROOT = DATA_ROOT / "birdnet"
DB_PATH = STATE_ROOT / "birdnet.db"
MIN_FREE_PERCENT = float(os.environ.get("BIRDNET_MIN_FREE_PERCENT", "10"))
TARGET_FREE_PERCENT = float(os.environ.get("BIRDNET_TARGET_FREE_PERCENT", "20"))
IDLE_SLEEP_SEC = int(os.environ.get("BIRDNET_THIN_IDLE_SLEEP_SEC", "60"))
ERROR_SLEEP_SEC = int(os.environ.get("BIRDNET_THIN_ERROR_SLEEP_SEC", "30"))
TRACE = False
INTERACTIVE_TEST_MODE = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Thin retained data when disk space is low."
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Ignore free-space thresholds, iteratively thin until no more candidates remain, then exit.",
    )
    return parser.parse_args()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def trace(message: str) -> None:
    if TRACE:
        print(f"TRACE: {message}")


def confirm_delete(path: Path) -> bool:
    response = input(f"Delete {path}? [y/N] ").strip()
    return response.lower() == "y"


def free_mb(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024 * 1024)


def free_percent(path: Path) -> float:
    usage = shutil.disk_usage(path)
    if usage.total <= 0:
        return 0.0
    return (usage.free / usage.total) * 100.0


def ensure_column(
    conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str
) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def connect_db() -> sqlite3.Connection:
    create_dir(str(STATE_ROOT), "sensos-admin", "sensos-data", 0o2775)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            channel_index INTEGER NOT NULL DEFAULT 0,
            window_index INTEGER NOT NULL,
            start_frame INTEGER NOT NULL,
            end_frame INTEGER NOT NULL,
            start_sec REAL NOT NULL,
            end_sec REAL NOT NULL,
            event_started_at TEXT,
            event_ended_at TEXT,
            window_volume REAL,
            top_label TEXT NOT NULL,
            top_score REAL NOT NULL,
            top_likely_score REAL,
            flac_path TEXT,
            deleted_at TEXT,
            UNIQUE (source_path, channel_index, window_index)
        )
        """
    )
    ensure_column(conn, "detections", "channel_index", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "detections", "event_started_at", "TEXT")
    ensure_column(conn, "detections", "event_ended_at", "TEXT")
    ensure_column(conn, "detections", "window_volume", "REAL")
    ensure_column(conn, "detections", "top_likely_score", "REAL")
    ensure_column(conn, "detections", "flac_path", "TEXT")
    ensure_column(conn, "detections", "deleted_at", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_detections_flac ON detections (deleted_at, flac_path)"
    )
    conn.commit()
    for path in (DB_PATH, DB_PATH.with_name(f"{DB_PATH.name}-wal"), DB_PATH.with_name(f"{DB_PATH.name}-shm")):
        if path.exists():
            try:
                path.chmod(0o664)
            except PermissionError:
                pass
    return conn


def mark_missing(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute(
        "UPDATE detections SET deleted_at = COALESCE(deleted_at, ?) WHERE id = ?",
        (now_iso(), run_id),
    )
    conn.commit()


def choose_victim_file(conn: sqlite3.Connection) -> tuple[int, Path] | None:
    rows = conn.execute(
        """
        SELECT id,
               flac_path,
               top_score,
               window_volume,
               start_sec,
               end_sec
        FROM detections
        WHERE deleted_at IS NULL
          AND flac_path IS NOT NULL
        ORDER BY id
        """
    ).fetchall()
    if not rows:
        trace("no BirdNET FLAC runs remain with undeleted files")
        return None

    grouped: dict[str, dict[str, float | int | list[tuple[int, str, float, float | None, float]]]] = {}
    for run_id, rel_path, top_score, window_volume, start_sec, end_sec in rows:
        label_dir = str(Path(rel_path).parent)
        duration_sec = float(end_sec) - float(start_sec)
        bucket = grouped.setdefault(
            label_dir,
            {"clip_count": 0, "total_duration_sec": 0.0, "rows": []},
        )
        bucket["clip_count"] = int(bucket["clip_count"]) + 1
        bucket["total_duration_sec"] = float(bucket["total_duration_sec"]) + duration_sec
        bucket["rows"].append((run_id, rel_path, float(top_score), None if window_volume is None else float(window_volume), duration_sec))

    ordered_dirs = sorted(
        grouped.items(),
        key=lambda item: (
            -float(item[1]["total_duration_sec"]),
            -int(item[1]["clip_count"]),
            item[0],
        ),
    )
    for label_dir, bucket in ordered_dirs:
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
        clip_count = int(bucket["clip_count"])
        total_duration_sec = float(bucket["total_duration_sec"])
        for run_id, rel_path, peak_score, peak_volume, duration_sec in candidate_rows:
            abs_path = AUDIO_ROOT / rel_path
            if abs_path.exists():
                trace(
                    "selected BirdNET file from fullest leaf: "
                    f"{abs_path} score={peak_score:.4f} "
                    f"volume={'na' if peak_volume is None else f'{peak_volume:.4f}'} "
                    f"duration={duration_sec:.3f}s "
                    f"label_dir={label_dir or 'na'} "
                    f"leaf_clip_count={clip_count} "
                    f"leaf_duration={total_duration_sec:.3f}s"
                )
                return run_id, abs_path
            mark_missing(conn, run_id)
    trace("all BirdNET thinning candidates were already missing on disk")
    return None


def prune_empty_dirs(start: Path) -> None:
    current = start
    while current != OUTPUT_ROOT and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def thin_once(conn: sqlite3.Connection) -> bool:
    victim = choose_victim_file(conn)
    if victim is None:
        return False

    run_id, victim_file = victim
    if INTERACTIVE_TEST_MODE and not confirm_delete(victim_file):
        print("Test thinning stopped by user.")
        raise SystemExit(0)
    print(f"Thinning {victim_file}")
    victim_file.unlink(missing_ok=True)
    conn.execute(
        "UPDATE detections SET deleted_at = COALESCE(deleted_at, ?) WHERE id = ?",
        (now_iso(), run_id),
    )
    conn.commit()
    prune_empty_dirs(victim_file.parent)
    return True

def main() -> None:
    global TRACE, INTERACTIVE_TEST_MODE

    args = parse_args()
    TRACE = args.test
    INTERACTIVE_TEST_MODE = args.test
    if not args.test:
        setup_logging("thin_data.log")
    conn = connect_db()
    print(
        "thin-data starting: "
        f"root={DATA_ROOT} min_free_percent={MIN_FREE_PERCENT:.1f} "
        f"target_free_percent={TARGET_FREE_PERCENT:.1f} "
        f"test={'yes' if args.test else 'no'} trace={'yes' if TRACE else 'no'}"
    )

    if args.test:
        deleted_count = 0
        print(
            "Test mode: thin processed BirdNET outputs from the leaf directory "
            "with the most retained audio, deleting the weakest clip within that leaf first."
        )
        while thin_once(conn):
            deleted_count += 1
        print(f"Test thinning complete. Deleted {deleted_count} file(s).")
        return

    while True:
        try:
            current_free_percent = free_percent(DATA_ROOT)
            if current_free_percent >= MIN_FREE_PERCENT:
                time.sleep(IDLE_SLEEP_SEC)
                continue

            print(
                f"Free space low: {current_free_percent:.1f}% < {MIN_FREE_PERCENT:.1f}%. Starting thinning."
            )
            while current_free_percent < TARGET_FREE_PERCENT:
                if not thin_once(conn):
                    print("No processed BirdNET audio files available to thin.", file=sys.stderr)
                    break
                current_free_percent = free_percent(DATA_ROOT)
            print(
                f"Thinning pass complete. Free space now {current_free_percent:.1f}% "
                f"(target {TARGET_FREE_PERCENT:.1f}%)"
            )
            time.sleep(IDLE_SLEEP_SEC)
        except Exception as exc:
            print(f"Thinning failure: {exc}", file=sys.stderr)
            time.sleep(ERROR_SLEEP_SEC)


if __name__ == "__main__":
    main()
