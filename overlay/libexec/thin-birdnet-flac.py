#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

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

DATA_ROOT = CLIENT_ROOT / "data"
AUDIO_ROOT = DATA_ROOT / "audio_recordings"
OUTPUT_ROOT = AUDIO_ROOT / "processed"
STATE_ROOT = DATA_ROOT / "birdnet"
DB_PATH = STATE_ROOT / "birdnet.db"
MIN_FREE_MB = int(os.environ.get("BIRDNET_MIN_FREE_MB", "100"))
TARGET_FREE_MB = int(os.environ.get("BIRDNET_TARGET_FREE_MB", "200"))
IDLE_SLEEP_SEC = int(os.environ.get("BIRDNET_THIN_IDLE_SLEEP_SEC", "60"))
ERROR_SLEEP_SEC = int(os.environ.get("BIRDNET_THIN_ERROR_SLEEP_SEC", "30"))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def free_mb(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024 * 1024)


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
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
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
        SELECT label_dir, COUNT(*) AS file_count
        FROM flac_runs
        WHERE deleted_at IS NULL
          AND label_dir IS NOT NULL
        GROUP BY label_dir
        ORDER BY file_count DESC
        """
    ).fetchall()
    if not rows:
        return None
    max_count = rows[0][1]
    candidates = [label_dir for label_dir, count in rows if count == max_count]
    return random.choice(candidates)


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
            return run_id, abs_path
        mark_missing(conn, run_id)
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
    label_dir = choose_victim_dir(conn)
    if label_dir is None:
        return False

    victim = choose_victim_file(conn, label_dir)
    if victim is None:
        return False

    run_id, victim_file = victim
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
    setup_logging("thin_birdnet_flac.log")
    conn = connect_db()

    while True:
        try:
            current_free_mb = free_mb(DATA_ROOT)
            if current_free_mb >= MIN_FREE_MB:
                time.sleep(IDLE_SLEEP_SEC)
                continue

            print(
                f"Free space low: {current_free_mb:.1f} MB < {MIN_FREE_MB} MB. Starting thinning."
            )
            while current_free_mb < TARGET_FREE_MB:
                if not thin_once(conn):
                    print("No FLAC files available to thin.", file=sys.stderr)
                    break
                current_free_mb = free_mb(DATA_ROOT)
            print(
                f"Thinning pass complete. Free space now {current_free_mb:.1f} MB "
                f"(target {TARGET_FREE_MB} MB)"
            )
            time.sleep(IDLE_SLEEP_SEC)
        except Exception as exc:
            print(f"Thinning failure: {exc}", file=sys.stderr)
            time.sleep(ERROR_SLEEP_SEC)


if __name__ == "__main__":
    main()
