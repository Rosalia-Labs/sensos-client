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


def backfill_flac_run_columns(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, flac_path
        FROM flac_runs
        WHERE label_dir IS NULL
        """
    ).fetchall()
    if not rows:
        return

    conn.executemany(
        "UPDATE flac_runs SET label_dir = ? WHERE id = ?",
        [(Path(flac_path).parent.as_posix(), row_id) for row_id, flac_path in rows],
    )


def connect_db() -> sqlite3.Connection:
    create_dir(str(STATE_ROOT), "sensos-admin", "sensos-data", 0o2775)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flac_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            run_index INTEGER NOT NULL,
            label TEXT NOT NULL,
            label_dir TEXT,
            start_frame INTEGER NOT NULL,
            end_frame INTEGER NOT NULL,
            start_sec REAL NOT NULL,
            end_sec REAL NOT NULL,
            peak_score REAL NOT NULL,
            peak_volume REAL,
            peak_likely_score REAL,
            flac_path TEXT NOT NULL,
            deleted_at TEXT,
            UNIQUE (source_path, run_index)
        )
        """
    )
    ensure_column(conn, "flac_runs", "label_dir", "TEXT")
    ensure_column(conn, "flac_runs", "peak_volume", "REAL")
    ensure_column(conn, "flac_runs", "deleted_at", "TEXT")
    backfill_flac_run_columns(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flac_runs_active_dir ON flac_runs (label_dir, deleted_at)"
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
        "UPDATE flac_runs SET deleted_at = COALESCE(deleted_at, ?) WHERE id = ?",
        (now_iso(), run_id),
    )
    conn.commit()


def choose_victim_file(conn: sqlite3.Connection) -> tuple[int, Path] | None:
    rows = conn.execute(
        """
        SELECT id,
               flac_path,
               label_dir,
               peak_score,
               peak_volume,
               (end_sec - start_sec) AS duration_sec
        FROM flac_runs
        WHERE deleted_at IS NULL
        ORDER BY peak_score ASC,
                 CASE WHEN peak_volume IS NULL THEN 1 ELSE 0 END ASC,
                 peak_volume ASC,
                 duration_sec DESC,
                 start_sec ASC,
                 id ASC
        """,
    ).fetchall()
    if not rows:
        trace("no BirdNET FLAC runs remain with undeleted files")
        return None
    for run_id, rel_path, label_dir, peak_score, peak_volume, duration_sec in rows:
        abs_path = AUDIO_ROOT / rel_path
        if abs_path.exists():
            trace(
                "selected low-confidence BirdNET file: "
                f"{abs_path} score={peak_score:.4f} "
                f"volume={'na' if peak_volume is None else f'{peak_volume:.4f}'} "
                f"duration={duration_sec:.3f}s "
                f"label_dir={label_dir or 'na'}"
            )
            return run_id, abs_path
        mark_missing(conn, run_id)
    trace("all low-confidence BirdNET candidates were already missing on disk")
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
        "UPDATE flac_runs SET deleted_at = COALESCE(deleted_at, ?) WHERE id = ?",
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
            "Test mode: thin processed BirdNET outputs by lowest confidence score, "
            "then lowest volume when scores tie."
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
