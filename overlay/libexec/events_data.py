# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

"""Local spool for infrequent client event reports.

Events are appended here by ``sensos-report-event`` (from boot units, PAM login
hooks, config commands, and OnFailure= handlers) and drained to the server by
``upload-events.py`` on a timer. The spool keeps events across offline periods;
each row carries a client-generated UUID so a retried upload is idempotent.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

CLIENT_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", "/sensos"))
DB_PATH = CLIENT_ROOT / "data" / "microenv" / "events.db"
CONFIG_PATH = CLIENT_ROOT / "etc" / "events.conf"

VALID_SEVERITIES = ("info", "notice", "warning")
SEVERITY_RANK = {"info": 0, "notice": 1, "warning": 2}
VALID_FLUSH_MODES = ("frequent", "hourly", "daily", "manual")

# Defaults used when /sensos/etc/events.conf is absent: event reporting works
# out of the box with no config file. config-events writes the file to change
# any of these.
DEFAULT_ENABLED = True
DEFAULT_MIN_SEVERITY = "info"
DEFAULT_FLUSH = "hourly"
DEFAULT_LOGIN_DEDUPE_SEC = 900
DEFAULT_RETENTION_DAYS = 30
KEEP_SENT_DAYS = DEFAULT_RETENTION_DAYS  # backwards-compatible alias

# sent_to_server states
STATE_QUEUED = 0
STATE_SENT = 1
STATE_SUPPRESSED = 2  # recorded locally, filtered by events.conf, never uploaded


def utcnow_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_events_config() -> dict[str, str]:
    config: dict[str, str] = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    except OSError:
        pass
    return config


def _config_bool(config: dict, key: str, default: bool) -> bool:
    raw = config.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("false", "0", "no", "off")


def _config_int(config: dict, key: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(config.get(key, default)))
    except (TypeError, ValueError):
        return default


def events_enabled(config: dict) -> bool:
    return _config_bool(config, "EVENTS_ENABLED", DEFAULT_ENABLED)


def min_severity_rank(config: dict) -> int:
    name = config.get("EVENTS_MIN_SEVERITY", DEFAULT_MIN_SEVERITY).strip().lower()
    return SEVERITY_RANK.get(name, 0)


def suppressed_types(config: dict) -> set[str]:
    return {t.strip() for t in config.get("EVENTS_SUPPRESS_TYPES", "").split(",") if t.strip()}


def login_dedupe_seconds(config: dict) -> int:
    return _config_int(config, "EVENTS_LOGIN_DEDUPE_SEC", DEFAULT_LOGIN_DEDUPE_SEC, minimum=0)


def retention_days(config: dict) -> int:
    return _config_int(config, "EVENTS_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, minimum=1)


def flush_mode(config: dict) -> str:
    mode = config.get("EVENTS_FLUSH", DEFAULT_FLUSH).strip().lower()
    return mode if mode in VALID_FLUSH_MODES else DEFAULT_FLUSH


def event_is_uploadable(config: dict, event_type: str, severity: str) -> bool:
    """Whether an event of this type/severity would ever be sent to the server
    under the current config. A non-uploadable event is still recorded locally
    (marked suppressed) for on-device inspection."""
    if not events_enabled(config):
        return False
    if event_type in suppressed_types(config):
        return False
    if SEVERITY_RANK.get(severity, 0) < min_severity_rank(config):
        return False
    return True


def _harden_permissions() -> None:
    """Keep the spool group-writable so root (PAM hooks) and sensos-runner
    (timer services) can both append. The microenv dir is setgid sensos-data,
    so new files already land in the right group; we only relax the mode."""
    for path in (
        DB_PATH,
        DB_PATH.with_name(f"{DB_PATH.name}-wal"),
        DB_PATH.with_name(f"{DB_PATH.name}-shm"),
    ):
        try:
            if path.exists():
                path.chmod(0o664)
        except OSError:
            pass


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_schema(conn)
    _harden_permissions()
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE,
            occurred_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            sent_to_server INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_pending ON events (sent_to_server, occurred_at, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_type_time ON events (event_type, occurred_at)"
    )
    conn.commit()


def recent_event_exists(
    conn: sqlite3.Connection,
    event_type: str,
    within_seconds: int,
    *,
    detail_key: str | None = None,
    detail_value: str | None = None,
) -> bool:
    """True when a matching event was recorded within the window. Used to
    rate-limit chatty sources such as SSH logins."""
    cutoff = datetime.now(timezone.utc).timestamp() - within_seconds
    rows = conn.execute(
        """
        SELECT created_at, details_json
        FROM events
        WHERE event_type = ?
        ORDER BY id DESC
        LIMIT 50
        """,
        (event_type,),
    ).fetchall()
    for row in rows:
        try:
            created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if created < cutoff:
            break
        if detail_key is None:
            return True
        try:
            details = json.loads(row["details_json"] or "{}")
        except json.JSONDecodeError:
            details = {}
        if str(details.get(detail_key)) == str(detail_value):
            return True
    return False


def insert_event(
    conn: sqlite3.Connection,
    event_type: str,
    severity: str,
    details: dict,
    *,
    suppressed: bool = False,
) -> str:
    event_uuid = str(uuid.uuid4())
    now = utcnow_text()
    state = STATE_SUPPRESSED if suppressed else STATE_QUEUED
    conn.execute(
        """
        INSERT INTO events
            (event_uuid, occurred_at, event_type, severity, details_json, created_at, sent_to_server)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (event_uuid, now, event_type, severity, json.dumps(details, sort_keys=True), now, state),
    )
    conn.commit()
    return event_uuid


def select_pending(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, event_uuid, occurred_at, event_type, severity, details_json
        FROM events
        WHERE sent_to_server = ?
        ORDER BY occurred_at, id
        LIMIT ?
        """,
        (STATE_QUEUED, limit),
    ).fetchall()


def mark_sent(conn: sqlite3.Connection, event_ids: list[int]) -> None:
    if not event_ids:
        return
    placeholders = ",".join("?" for _ in event_ids)
    conn.execute(
        f"UPDATE events SET sent_to_server = {STATE_SENT} WHERE id IN ({placeholders})",
        tuple(event_ids),
    )
    conn.commit()


def suppress_queued(conn: sqlite3.Connection) -> int:
    """Mark every still-queued event as suppressed. Used when config-events
    disables reporting so a later re-enable does not flush a stale backlog."""
    cur = conn.execute(
        f"UPDATE events SET sent_to_server = {STATE_SUPPRESSED} WHERE sent_to_server = {STATE_QUEUED}"
    )
    conn.commit()
    return cur.rowcount


def prune_sent(conn: sqlite3.Connection, keep_days: int = DEFAULT_RETENTION_DAYS) -> None:
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    cutoff_text = datetime.fromtimestamp(cutoff, timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    conn.execute(
        "DELETE FROM events WHERE sent_to_server != ? AND created_at < ?",
        (STATE_QUEUED, cutoff_text),
    )
    conn.commit()
