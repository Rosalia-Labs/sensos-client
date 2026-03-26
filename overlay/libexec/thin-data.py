#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import argparse
import importlib.util
import os
import random
import shutil
import sqlite3
import sys
import time
from pathlib import Path

SCRIPT_FILE = os.path.realpath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_FILE)
DEFAULT_OVERLAY_ROOT = os.path.dirname(SCRIPT_DIR)


def resolve_overlay_root() -> str:
    candidates = []
    env_root = os.environ.get("SENSOS_CLIENT_ROOT")
    if env_root:
        candidates.append(env_root)
    candidates.extend([DEFAULT_OVERLAY_ROOT, "/sensos"])

    for candidate in candidates:
        utils_file = os.path.join(candidate, "libexec", "utils.py")
        if os.path.isfile(utils_file):
            return candidate

    return DEFAULT_OVERLAY_ROOT


OVERLAY_ROOT = resolve_overlay_root()
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
COMPRESSED_ROOT = AUDIO_ROOT / "compressed"
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
            peak_likely_score REAL,
            flac_path TEXT NOT NULL,
            deleted_at TEXT,
            UNIQUE (source_path, run_index)
        )
        """
    )
    ensure_column(conn, "flac_runs", "label_dir", "TEXT")
    ensure_column(conn, "flac_runs", "deleted_at", "TEXT")
    backfill_flac_run_columns(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flac_runs_active_dir ON flac_runs (label_dir, deleted_at)"
    )
    conn.commit()
    for path in (DB_PATH, DB_PATH.with_name(f"{DB_PATH.name}-wal"), DB_PATH.with_name(f"{DB_PATH.name}-shm")):
        if path.exists():
            path.chmod(0o664)
    return conn


def mark_missing(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute(
        "UPDATE flac_runs SET deleted_at = COALESCE(deleted_at, ?) WHERE id = ?",
        (now_iso(), run_id),
    )
    conn.commit()


def choose_victim_dir(conn: sqlite3.Connection) -> str | None:
    rows = conn.execute(
        """
        SELECT
            label_dir,
            COUNT(*) AS file_count,
            COALESCE(SUM(end_sec - start_sec), 0) AS total_seconds
        FROM flac_runs
        WHERE deleted_at IS NULL
          AND label_dir IS NOT NULL
        GROUP BY label_dir
        ORDER BY total_seconds DESC, file_count DESC
        """
    ).fetchall()
    if not rows:
        trace("no labelled FLAC directories remain with undeleted files")
        return None
    max_seconds = rows[0][2]
    candidates = [
        (label_dir, file_count)
        for label_dir, file_count, total_seconds in rows
        if total_seconds == max_seconds
    ]
    chosen, chosen_file_count = random.choice(candidates)
    trace(
        "selected victim directory: "
        f"{chosen} with {max_seconds:.3f} retained second(s) across {chosen_file_count} file(s); "
        f"{len(candidates)} director{'y' if len(candidates) == 1 else 'ies'} tied for largest retained duration"
    )
    return chosen


def choose_victim_file(conn: sqlite3.Connection, label_dir: str) -> tuple[int, Path] | None:
    rows = conn.execute(
        """
        SELECT id, flac_path
        FROM flac_runs
        WHERE deleted_at IS NULL
          AND label_dir = ?
        ORDER BY RANDOM()
        """,
        (label_dir,),
    ).fetchall()
    for run_id, rel_path in rows:
        abs_path = AUDIO_ROOT / rel_path
        if abs_path.exists():
            trace(f"selected random file from chosen directory: {abs_path}")
            return run_id, abs_path
        mark_missing(conn, run_id)
    trace(f"chosen directory has no remaining files on disk: {label_dir}")
    return None


def choose_compressed_victim_dir() -> Path | None:
    if not COMPRESSED_ROOT.exists():
        trace("compressed root does not exist")
        return None

    leaf_counts = []
    for dir_path in sorted(path for path in COMPRESSED_ROOT.rglob("*") if path.is_dir()):
        files = [path for path in dir_path.iterdir() if path.is_file() and path.suffix.lower() == ".flac"]
        if not files:
            continue
        leaf_counts.append((dir_path, len(files)))

    if not leaf_counts:
        trace("no compressed FLAC directories remain with files")
        return None

    max_count = max(file_count for _, file_count in leaf_counts)
    candidates = [(dir_path, file_count) for dir_path, file_count in leaf_counts if file_count == max_count]
    chosen_dir, chosen_count = random.choice(candidates)
    trace(
        "selected compressed victim directory: "
        f"{chosen_dir} with {chosen_count} file(s); "
        f"{len(candidates)} director{'y' if len(candidates) == 1 else 'ies'} tied for largest file count"
    )
    return chosen_dir


def choose_compressed_victim_file(dir_path: Path) -> Path | None:
    files = [path for path in dir_path.iterdir() if path.is_file() and path.suffix.lower() == ".flac"]
    if not files:
        trace(f"chosen compressed directory has no remaining files: {dir_path}")
        return None

    victim_file = random.choice(files)
    trace(f"selected random compressed file from chosen directory: {victim_file}")
    return victim_file


def prune_empty_compressed_dirs(start: Path) -> None:
    current = start
    while current != COMPRESSED_ROOT and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def thin_compressed_once() -> bool:
    victim_dir = choose_compressed_victim_dir()
    if victim_dir is None:
        return False

    victim_file = choose_compressed_victim_file(victim_dir)
    if victim_file is None:
        return False

    if INTERACTIVE_TEST_MODE and not confirm_delete(victim_file):
        print("Test thinning stopped by user.")
        raise SystemExit(0)
    print(f"Thinning compressed {victim_file}")
    victim_file.unlink(missing_ok=True)
    prune_empty_compressed_dirs(victim_file.parent)
    return True


def prune_empty_dirs(start: Path) -> None:
    current = start
    while current != OUTPUT_ROOT and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def thin_once(conn: sqlite3.Connection) -> bool:
    label_dir = choose_victim_dir(conn)
    if label_dir is None:
        return False

    victim = choose_victim_file(conn, label_dir)
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


def thin_once_with_fallback(conn: sqlite3.Connection) -> bool:
    if thin_once(conn):
        return True

    trace(
        "processed directory thinning exhausted before reaching target; "
        "falling back to compressed directory thinning"
    )
    return thin_compressed_once()


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
            "Test mode: first thin processed outputs by largest retained duration, "
            "then fall back to compressed inputs by largest file-count directory if needed."
        )
        while thin_once_with_fallback(conn):
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
                if not thin_once_with_fallback(conn):
                    print("No processed or compressed audio files available to thin.", file=sys.stderr)
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
