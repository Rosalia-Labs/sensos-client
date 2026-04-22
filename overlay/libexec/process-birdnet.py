#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import importlib.util
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List

import numpy as np
import soundfile as sf

SCRIPT_FILE = os.path.realpath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_FILE)
OVERLAY_ROOT = os.environ.get("SENSOS_CLIENT_ROOT", "/sensos")
CLIENT_ROOT = os.environ.get("SENSOS_CLIENT_ROOT", OVERLAY_ROOT)
UTILS_FILE = os.path.join(OVERLAY_ROOT, "libexec", "utils.py")

if not os.path.isfile(UTILS_FILE):
    raise RuntimeError(f"Missing utils.py at {UTILS_FILE}")

UTILS_SPEC = importlib.util.spec_from_file_location("sensos_overlay_utils", UTILS_FILE)
UTILS_MODULE = importlib.util.module_from_spec(UTILS_SPEC)
assert UTILS_SPEC.loader is not None
UTILS_SPEC.loader.exec_module(UTILS_MODULE)

read_kv_config = UTILS_MODULE.read_kv_config
setup_logging = UTILS_MODULE.setup_logging
create_dir = UTILS_MODULE.create_dir

CLIENT_ROOT_PATH = Path(CLIENT_ROOT)
INPUT_ROOT = CLIENT_ROOT_PATH / "data" / "audio_recordings" / "compressed"
OUTPUT_ROOT = CLIENT_ROOT_PATH / "data" / "audio_recordings" / "processed"
STATE_ROOT = CLIENT_ROOT_PATH / "data" / "birdnet"
DB_PATH = STATE_ROOT / "birdnet.db"
MODEL_ROOT = CLIENT_ROOT_PATH / "birdnet" / "BirdNET_v2.4_tflite"
MODEL_PATH = MODEL_ROOT / "audio-model.tflite"
META_MODEL_PATH = MODEL_ROOT / "meta-model.tflite"
LABELS_PATH = MODEL_ROOT / "labels" / "en_us.txt"
LOCATION_CONF = CLIENT_ROOT_PATH / "etc" / "location.conf"
BIRDNET_CONFIG = CLIENT_ROOT_PATH / "etc" / "birdnet.env"

WINDOW_SEC = 3
STRIDE_SEC = 1
SAMPLE_RATE = 48000
WINDOW_FRAMES = WINDOW_SEC * SAMPLE_RATE
STRIDE_FRAMES = STRIDE_SEC * SAMPLE_RATE
MIN_FILE_AGE_SEC = int(os.environ.get("BIRDNET_MIN_FILE_AGE_SEC", "15"))
FILE_STABLE_SEC = int(os.environ.get("BIRDNET_FILE_STABLE_SEC", "30"))
IDLE_SLEEP_SEC = int(os.environ.get("BIRDNET_IDLE_SLEEP_SEC", "60"))
ERROR_SLEEP_SEC = int(os.environ.get("BIRDNET_ERROR_SLEEP_SEC", "10"))


def read_birdnet_config(config_path: Path) -> dict[str, str]:
    config: dict[str, str] = {}
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()
    except FileNotFoundError:
        return config
    return config


def read_backend_preference(config_path: Path) -> str:
    backend = "litert"
    config = read_birdnet_config(config_path)
    candidate = config.get("BIRDNET_BACKEND")
    if candidate:
        backend = candidate

    if backend == "tflite":
        backend = "litert"
    if backend not in {"tensorflow", "litert"}:
        raise RuntimeError(f"Unsupported BIRDNET_BACKEND='{backend}' in {config_path}")
    return backend


def read_input_mode(config_path: Path) -> str:
    config = read_birdnet_config(config_path)
    mode = config.get("BIRDNET_INPUT_MODE", "mono")
    if mode not in {"mono", "split-channels"}:
        raise RuntimeError(f"Unsupported BIRDNET_INPUT_MODE='{mode}' in {config_path}")
    return mode


BACKEND_PREFERENCE = read_backend_preference(BIRDNET_CONFIG)
INPUT_MODE = read_input_mode(BIRDNET_CONFIG)
if BACKEND_PREFERENCE == "litert":
    try:
        from ai_edge_litert.interpreter import Interpreter
    except ImportError as exc:
        raise RuntimeError(
            "BirdNET backend is configured as 'litert', but ai-edge-litert is not installed."
        ) from exc

    class _LiteRTModule:
        Interpreter = Interpreter

    tflite = _LiteRTModule()
    INTERPRETER_BACKEND = "litert"
else:
    try:
        import tensorflow as tf
    except ImportError as exc:
        raise RuntimeError(
            "BirdNET backend is configured as 'tensorflow', but tensorflow is not installed."
        ) from exc

    class _TensorFlowLiteModule:
        Interpreter = tf.lite.Interpreter

    tflite = _TensorFlowLiteModule()
    INTERPRETER_BACKEND = "tensorflow"


@dataclass
class BirdNETModel:
    interpreter: tflite.Interpreter
    input_details: list
    output_details: list
    labels: List[str]


@dataclass
class Detection:
    channel_index: int
    window_index: int
    start_frame: int
    end_frame: int
    window_volume: float
    label: str
    score: float
    likely_score: float | None


@dataclass
class LabelRun:
    channel_index: int
    run_index: int
    start_frame: int
    end_frame: int
    label: str
    peak_score: float
    peak_volume: float
    peak_likely_score: float | None


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_birdnet_model(model_path: Path, labels_path: Path) -> BirdNETModel:
    interpreter = tflite.Interpreter(model_path=str(model_path))
    interpreter.allocate_tensors()
    with labels_path.open("r", encoding="utf-8") as f:
        labels = [
            f"{common} ({sci})" if "_" in line else line.strip()
            for line in f.readlines()
            for sci, common in [line.strip().split("_", 1)]
        ]
    return BirdNETModel(
        interpreter=interpreter,
        input_details=interpreter.get_input_details(),
        output_details=interpreter.get_output_details(),
        labels=labels,
    )


def flat_sigmoid(x: np.ndarray, sensitivity: float = -1, bias: float = 1.0) -> np.ndarray:
    return 1 / (1.0 + np.exp(sensitivity * np.clip((x + (bias - 1.0) * 10.0), -20, 20)))


def scale_by_max_value(audio: np.ndarray) -> np.ndarray:
    max_val = np.max(np.abs(audio))
    if max_val == 0:
        return np.zeros_like(audio, dtype=np.float32)
    scale = max_val * (32768.0 / 32767.0)
    return (audio / scale).astype(np.float32)


def normalized_window_volume(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    normalized = audio.astype(np.float64) / float(np.iinfo(np.int32).max)
    rms = float(np.sqrt(np.mean(np.square(normalized), dtype=np.float64)))
    return min(max(rms, 0.0), 1.0)


def invoke_birdnet_top_label(
    audio: np.ndarray,
    model: BirdNETModel,
    meta_model: BirdNETModel | None,
    latitude: float | None,
    longitude: float | None,
    observed_on: date,
) -> tuple[str, float, float | None]:
    input_data = np.expand_dims(audio, axis=0).astype(np.float32)
    model.interpreter.set_tensor(model.input_details[0]["index"], input_data)
    model.interpreter.invoke()
    scores = model.interpreter.get_tensor(model.output_details[0]["index"])
    scores_flat = flat_sigmoid(scores.flatten())
    top_index = int(np.argmax(scores_flat))
    likely_score = None
    if (
        meta_model is not None
        and latitude is not None
        and longitude is not None
        and not (latitude == 0 and longitude == 0)
    ):
        week = min(max(observed_on.isocalendar()[1], 1), 48)
        sample = np.expand_dims(
            np.array([latitude, longitude, week], dtype=np.float32), 0
        )
        meta_model.interpreter.set_tensor(meta_model.input_details[0]["index"], sample)
        meta_model.interpreter.invoke()
        likely_scores = meta_model.interpreter.get_tensor(
            meta_model.output_details[0]["index"]
        )[0]
        likely_score = float(likely_scores[top_index])
    return model.labels[top_index], float(scores_flat[top_index]), likely_score


def ensure_runtime_dirs() -> None:
    create_dir(str(STATE_ROOT), "sensos-admin", "sensos-data", 0o2775)
    create_dir(str(OUTPUT_ROOT), "sensos-admin", "sensos-data", 0o2775)


def ensure_state_file_permissions() -> None:
    for path in (DB_PATH, DB_PATH.with_name(f"{DB_PATH.name}-wal"), DB_PATH.with_name(f"{DB_PATH.name}-shm")):
        if path.exists():
            try:
                path.chmod(0o664)
            except PermissionError:
                pass


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_files (
            source_path TEXT PRIMARY KEY,
            sample_rate INTEGER,
            channels INTEGER,
            frames INTEGER,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            processed_at TEXT,
            status TEXT NOT NULL,
            error TEXT,
            output_dir TEXT,
            deleted_source INTEGER NOT NULL DEFAULT 0
        )
        """
    )
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
            UNIQUE (source_path, channel_index, window_index)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flac_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            channel_index INTEGER NOT NULL DEFAULT 0,
            run_index INTEGER NOT NULL,
            label TEXT NOT NULL,
            label_dir TEXT,
            start_frame INTEGER NOT NULL,
            end_frame INTEGER NOT NULL,
            start_sec REAL NOT NULL,
            end_sec REAL NOT NULL,
            event_started_at TEXT,
            event_ended_at TEXT,
            peak_score REAL NOT NULL,
            peak_volume REAL,
            peak_likely_score REAL,
            flac_path TEXT NOT NULL,
            deleted_at TEXT,
            UNIQUE (source_path, channel_index, run_index)
        )
        """
    )
    ensure_column(conn, "processed_files", "ended_at", "TEXT")
    ensure_column(conn, "detections", "channel_index", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "detections", "event_started_at", "TEXT")
    ensure_column(conn, "detections", "event_ended_at", "TEXT")
    ensure_column(conn, "detections", "window_volume", "REAL")
    ensure_column(conn, "detections", "top_likely_score", "REAL")
    ensure_column(conn, "flac_runs", "channel_index", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "flac_runs", "event_started_at", "TEXT")
    ensure_column(conn, "flac_runs", "event_ended_at", "TEXT")
    ensure_column(conn, "flac_runs", "label_dir", "TEXT")
    ensure_column(conn, "flac_runs", "peak_volume", "REAL")
    ensure_column(conn, "flac_runs", "peak_likely_score", "REAL")
    ensure_column(conn, "flac_runs", "deleted_at", "TEXT")
    backfill_flac_run_columns(conn)
    backfill_recording_timestamps(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_detections_source ON detections (source_path, window_index)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_detections_event ON detections (event_started_at, channel_index)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flac_runs_source ON flac_runs (source_path, run_index)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flac_runs_active_dir ON flac_runs (label_dir, deleted_at)"
    )
    conn.commit()
    ensure_state_file_permissions()
    return conn


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
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


def backfill_recording_timestamps(conn: sqlite3.Connection) -> None:
    processed_updates = []
    for source_path, sample_rate, frames, started_at, ended_at in conn.execute(
        """
        SELECT source_path, sample_rate, frames, started_at, ended_at
        FROM processed_files
        """
    ):
        source_start = source_start_datetime(source_path)
        if source_start is None:
            continue
        normalized_start = iso_utc_text(source_start)
        normalized_end = frame_time_text(source_path, frames, sample_rate)
        if started_at != normalized_start or ended_at != normalized_end:
            processed_updates.append((normalized_start, normalized_end, source_path))

    if processed_updates:
        conn.executemany(
            """
            UPDATE processed_files
            SET started_at = ?, ended_at = ?
            WHERE source_path = ?
            """,
            processed_updates,
        )

    detection_updates = []
    for row_id, source_path, start_frame, end_frame in conn.execute(
        """
        SELECT id, source_path, start_frame, end_frame
        FROM detections
        """
    ):
        event_started_at = frame_time_text(source_path, start_frame, SAMPLE_RATE)
        event_ended_at = frame_time_text(source_path, end_frame, SAMPLE_RATE)
        if event_started_at is None or event_ended_at is None:
            continue
        detection_updates.append((event_started_at, event_ended_at, row_id))

    if detection_updates:
        conn.executemany(
            """
            UPDATE detections
            SET event_started_at = ?, event_ended_at = ?
            WHERE id = ?
            """,
            detection_updates,
        )

    flac_run_updates = []
    for row_id, source_path, start_frame, end_frame in conn.execute(
        """
        SELECT id, source_path, start_frame, end_frame
        FROM flac_runs
        """
    ):
        event_started_at = frame_time_text(source_path, start_frame, SAMPLE_RATE)
        event_ended_at = frame_time_text(source_path, end_frame, SAMPLE_RATE)
        if event_started_at is None or event_ended_at is None:
            continue
        flac_run_updates.append((event_started_at, event_ended_at, row_id))

    if flac_run_updates:
        conn.executemany(
            """
            UPDATE flac_runs
            SET event_started_at = ?, event_ended_at = ?
            WHERE id = ?
            """,
            flac_run_updates,
        )


def was_processed_successfully(conn: sqlite3.Connection, path: Path) -> bool:
    row = conn.execute(
        "SELECT status FROM processed_files WHERE source_path = ?",
        (relative_source(path),),
    ).fetchone()
    return row is not None and row[0] == "done"


def find_next_audio(conn: sqlite3.Connection) -> Path | None:
    if not INPUT_ROOT.exists():
        return None
    now = time.time()
    candidates = []
    for path in INPUT_ROOT.rglob("*.flac"):
        try:
            age = now - path.stat().st_mtime
        except FileNotFoundError:
            continue
        if age >= MIN_FILE_AGE_SEC:
            if was_processed_successfully(conn, path):
                continue
            candidates.append(path)
    return min(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def is_file_stable(path: Path) -> bool:
    try:
        first = path.stat()
    except FileNotFoundError:
        return False

    if (time.time() - first.st_mtime) < MIN_FILE_AGE_SEC:
        return False

    time.sleep(FILE_STABLE_SEC)

    try:
        second = path.stat()
    except FileNotFoundError:
        return False

    return (
        first.st_size == second.st_size
        and first.st_mtime == second.st_mtime
        and (time.time() - second.st_mtime) >= MIN_FILE_AGE_SEC
    )


def relative_source(path: Path) -> str:
    return path.relative_to(INPUT_ROOT.parent).as_posix()


def sanitize_label(label: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", label.strip()).strip("._-")
    return slug or "unknown"


def is_human_label(label: str) -> bool:
    return "human" in label.lower()


def label_output_dir(source_path: Path, label: str) -> Path:
    rel = source_path.relative_to(INPUT_ROOT)
    return OUTPUT_ROOT / rel.parent / sanitize_label(label)


def format_coord(value: str | None, positive: str, negative: str) -> str:
    if value is None:
        return "na"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "na"
    direction = positive if numeric >= 0 else negative
    scaled = int(round(abs(numeric) * 10000))
    return f"{direction}{scaled:07d}"


def location_token() -> str:
    config = read_kv_config(str(LOCATION_CONF))
    lat = format_coord(config.get("LATITUDE"), "N", "S")
    lon = format_coord(config.get("LONGITUDE"), "E", "W")
    return f"{lat}_{lon}"


def location_coordinates() -> tuple[float | None, float | None]:
    config = read_kv_config(str(LOCATION_CONF))
    try:
        latitude = float(config["LATITUDE"])
        longitude = float(config["LONGITUDE"])
    except (KeyError, TypeError, ValueError):
        return None, None
    return latitude, longitude


def source_start_datetime(source_path: Path) -> datetime | None:
    path_obj = Path(source_path)
    match = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)", path_obj.stem)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iso_utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def require_source_start_datetime(source_path: Path) -> datetime:
    start_dt = source_start_datetime(source_path)
    if start_dt is None:
        raise ValueError(f"Could not parse recording start time from {source_path}")
    return start_dt


def frame_time_text(source_path: Path | str, frame_offset: int | None, sample_rate: int | None) -> str | None:
    if frame_offset is None or sample_rate in (None, 0):
        return None
    start_dt = source_start_datetime(source_path)
    if start_dt is None:
        return None
    return iso_utc_text(start_dt + timedelta(seconds=(frame_offset / sample_rate)))


def filename_time_token(source_path: Path, run: LabelRun, sample_rate: int) -> str:
    start_dt = source_start_datetime(source_path)
    if start_dt is None:
        return source_path.stem
    run_dt = start_dt + timedelta(seconds=(run.start_frame / sample_rate))
    return run_dt.strftime("%Y-%m-%dT%H-%M-%SZ")


def source_observation_date(source_path: Path) -> date:
    start_dt = source_start_datetime(source_path)
    if start_dt is None:
        return datetime.now(timezone.utc).date()
    return start_dt.date()


def format_score_token(value: float | None, prefix: str) -> str:
    if value is None:
        return f"{prefix}na"
    return f"{prefix}{value:.3f}"


def to_mono(audio: np.ndarray) -> np.ndarray:
    if audio.ndim == 1:
        return audio.astype(np.float32)
    return audio.astype(np.float32).mean(axis=1)


def audio_channels(audio: np.ndarray, input_mode: str) -> list[tuple[int, np.ndarray]]:
    if audio.ndim == 1:
        return [(0, audio.astype(np.float32))]
    if input_mode == "split-channels":
        return [(idx, audio[:, idx].astype(np.float32)) for idx in range(audio.shape[1])]
    return [(0, to_mono(audio))]


def collect_detections(
    channel_index: int,
    audio_mono: np.ndarray,
    frames: int,
    model: BirdNETModel,
    meta_model: BirdNETModel | None,
    latitude: float | None,
    longitude: float | None,
    observed_on: date,
) -> List[Detection]:
    detections: List[Detection] = []
    if frames < WINDOW_FRAMES:
        padded = np.zeros(WINDOW_FRAMES, dtype=np.float32)
        padded[:frames] = audio_mono[:frames]
        window_volume = normalized_window_volume(audio_mono[:frames])
        label, score, likely_score = invoke_birdnet_top_label(
            scale_by_max_value(padded),
            model,
            meta_model,
            latitude,
            longitude,
            observed_on,
        )
        return [
            Detection(
                channel_index,
                0,
                0,
                frames,
                window_volume,
                label,
                score,
                likely_score,
            )
        ]

    window_index = 0
    for start in range(0, frames - WINDOW_FRAMES + 1, STRIDE_FRAMES):
        end = start + WINDOW_FRAMES
        window_audio = audio_mono[start:end]
        label, score, likely_score = invoke_birdnet_top_label(
            scale_by_max_value(window_audio),
            model,
            meta_model,
            latitude,
            longitude,
            observed_on,
        )
        detections.append(
            Detection(
                channel_index,
                window_index,
                start,
                end,
                normalized_window_volume(window_audio),
                label,
                score,
                likely_score,
            )
        )
        window_index += 1
    return detections


def build_runs(detections: List[Detection]) -> List[LabelRun]:
    if not detections:
        return []

    runs: List[LabelRun] = []
    current = LabelRun(
        channel_index=detections[0].channel_index,
        run_index=0,
        start_frame=detections[0].start_frame,
        end_frame=detections[0].end_frame,
        label=detections[0].label,
        peak_score=detections[0].score,
        peak_volume=detections[0].window_volume,
        peak_likely_score=detections[0].likely_score,
    )

    for detection in detections[1:]:
        same_label = detection.label == current.label
        overlaps = detection.start_frame <= current.end_frame
        if same_label and overlaps:
            current.end_frame = max(current.end_frame, detection.end_frame)
            current.peak_score = max(current.peak_score, detection.score)
            current.peak_volume = max(current.peak_volume, detection.window_volume)
            if detection.likely_score is not None:
                if current.peak_likely_score is None:
                    current.peak_likely_score = detection.likely_score
                else:
                    current.peak_likely_score = max(
                        current.peak_likely_score, detection.likely_score
                    )
            continue

        runs.append(current)
        current = LabelRun(
            channel_index=detection.channel_index,
            run_index=len(runs),
            start_frame=detection.start_frame,
            end_frame=detection.end_frame,
            label=detection.label,
            peak_score=detection.score,
            peak_volume=detection.window_volume,
            peak_likely_score=detection.likely_score,
        )

    runs.append(current)
    return runs


def write_flac_runs(source_path: Path, audio: np.ndarray, sample_rate: int, runs: List[LabelRun]) -> List[tuple[LabelRun, Path]]:
    written = []
    loc_token = location_token()
    for run in runs:
        if is_human_label(run.label):
            continue
        out_dir = label_output_dir(source_path, run.label)
        create_dir(str(out_dir), "sensos-admin", "sensos-data", 0o2775)
        start_sec = run.start_frame / sample_rate
        end_sec = run.end_frame / sample_rate
        filename = (
            f"{filename_time_token(source_path, run, sample_rate)}_"
            f"{loc_token}_"
            f"ch{run.channel_index:02d}_"
            f"{run.run_index:03d}_"
            f"{sanitize_label(run.label)}_"
            f"{format_score_token(run.peak_score, 's')}_"
            f"{format_score_token(run.peak_likely_score, 'o')}_"
            f"{start_sec:09.3f}-{end_sec:09.3f}.flac"
        )
        flac_path = out_dir / filename
        if audio.ndim == 1:
            chunk = audio[run.start_frame : run.end_frame]
        else:
            chunk = audio[run.start_frame : run.end_frame, run.channel_index]
        sf.write(flac_path, chunk, sample_rate, format="FLAC")
        written.append((run, flac_path))
    return written


def record_failure(conn: sqlite3.Connection, source_key: str, info: sf.SoundFile | None, error: str) -> None:
    sample_rate = getattr(info, "samplerate", None)
    channels = getattr(info, "channels", None)
    frames = getattr(info, "frames", None)
    recording_started_at = frame_time_text(source_key, 0, 1)
    if recording_started_at is None:
        raise ValueError(f"Could not derive recording start time for {source_key}")
    recording_ended_at = frame_time_text(source_key, frames, sample_rate)
    conn.execute(
        """
        INSERT INTO processed_files (
            source_path, sample_rate, channels, frames, started_at, ended_at, processed_at, status, error, deleted_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(source_path) DO UPDATE SET
            sample_rate=excluded.sample_rate,
            channels=excluded.channels,
            frames=excluded.frames,
            started_at=excluded.started_at,
            ended_at=excluded.ended_at,
            processed_at=excluded.processed_at,
            status=excluded.status,
            error=excluded.error,
            deleted_source=0
        """,
        (
            source_key,
            sample_rate,
            channels,
            frames,
            recording_started_at,
            recording_ended_at,
            now_iso(),
            "error",
            error,
        ),
    )
    conn.commit()


def update_deleted_source(conn: sqlite3.Connection, source_key: str, source_path: Path) -> None:
    conn.execute(
        "UPDATE processed_files SET deleted_source = ? WHERE source_path = ?",
        (0 if source_path.exists() else 1, source_key),
    )
    conn.commit()


def delete_source(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        print(f"🗑️ Deleted source file {path}")
    except Exception as unlink_error:
        print(f"⚠️ Failed to delete source file {path}: {unlink_error}", file=sys.stderr)


def process_audio(
    model: BirdNETModel,
    meta_model: BirdNETModel | None,
    conn: sqlite3.Connection,
    source_path: Path,
) -> None:
    source_key = relative_source(source_path)
    info = sf.info(source_path)
    recording_started_at = iso_utc_text(require_source_start_datetime(source_path))
    recording_ended_at = frame_time_text(source_path, info.frames, info.samplerate)
    if info.samplerate != SAMPLE_RATE:
        raise ValueError(
            f"Unsupported sample rate {info.samplerate} for {source_key}; expected {SAMPLE_RATE}"
        )

    conn.execute(
        """
        INSERT INTO processed_files (
            source_path, sample_rate, channels, frames, started_at, ended_at, status, error, output_dir, deleted_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0)
        ON CONFLICT(source_path) DO UPDATE SET
            sample_rate=excluded.sample_rate,
            channels=excluded.channels,
            frames=excluded.frames,
            started_at=excluded.started_at,
            ended_at=excluded.ended_at,
            processed_at=NULL,
            status=excluded.status,
            error=NULL,
            output_dir=NULL,
            deleted_source=0
        """,
        (
            source_key,
            info.samplerate,
            info.channels,
            info.frames,
            recording_started_at,
            recording_ended_at,
            "processing",
        ),
    )
    conn.execute("DELETE FROM detections WHERE source_path = ?", (source_key,))
    conn.execute("DELETE FROM flac_runs WHERE source_path = ?", (source_key,))
    conn.commit()

    audio, sample_rate = sf.read(source_path, dtype="int32", always_2d=True)
    latitude, longitude = location_coordinates()
    detections: List[Detection] = []
    runs: List[LabelRun] = []
    for channel_index, channel_audio in audio_channels(audio, INPUT_MODE):
        channel_detections = collect_detections(
            channel_index,
            channel_audio,
            len(channel_audio),
            model,
            meta_model,
            latitude,
            longitude,
            source_observation_date(source_path),
        )
        detections.extend(channel_detections)
        runs.extend(build_runs(channel_detections))
    written_runs = write_flac_runs(source_path, audio, sample_rate, runs)

    conn.executemany(
        """
        INSERT INTO detections (
            source_path, channel_index, window_index, start_frame, end_frame, start_sec, end_sec, event_started_at, event_ended_at, window_volume, top_label, top_score, top_likely_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                source_key,
                d.channel_index,
                d.window_index,
                d.start_frame,
                d.end_frame,
                d.start_frame / sample_rate,
                d.end_frame / sample_rate,
                frame_time_text(source_path, d.start_frame, sample_rate),
                frame_time_text(source_path, d.end_frame, sample_rate),
                d.window_volume,
                d.label,
                d.score,
                d.likely_score,
            )
            for d in detections
        ],
    )
    conn.executemany(
        """
        INSERT INTO flac_runs (
            source_path, channel_index, run_index, label, label_dir, start_frame, end_frame, start_sec, end_sec, event_started_at, event_ended_at, peak_score, peak_volume, peak_likely_score, flac_path, deleted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        [
            (
                source_key,
                run.channel_index,
                run.run_index,
                run.label,
                flac_path.relative_to(INPUT_ROOT.parent).parent.as_posix(),
                run.start_frame,
                run.end_frame,
                run.start_frame / sample_rate,
                run.end_frame / sample_rate,
                frame_time_text(source_path, run.start_frame, sample_rate),
                frame_time_text(source_path, run.end_frame, sample_rate),
                run.peak_score,
                run.peak_volume,
                run.peak_likely_score,
                flac_path.relative_to(INPUT_ROOT.parent).as_posix(),
            )
            for run, flac_path in written_runs
        ],
    )
    conn.execute(
        """
        UPDATE processed_files
        SET processed_at = ?, status = ?, error = NULL, output_dir = ?
        WHERE source_path = ?
        """,
        (
            now_iso(),
            "done",
            source_path.relative_to(INPUT_ROOT).parent.as_posix(),
            source_key,
        ),
    )
    conn.commit()

    delete_source(source_path)
    update_deleted_source(conn, source_key, source_path)


def main() -> None:
    setup_logging("process_birdnet.log")
    ensure_runtime_dirs()
    conn = connect_db()
    model = None
    meta_model = None

    while True:
        next_audio = None
        try:
            if not MODEL_PATH.exists() or not LABELS_PATH.exists():
                print(f"⚠️ BirdNET model files missing under {MODEL_ROOT}. Sleeping...")
                time.sleep(IDLE_SLEEP_SEC)
                continue

            if model is None:
                print(f"🧠 Loading BirdNET model from {MODEL_PATH} using {INTERPRETER_BACKEND}")
                model = load_birdnet_model(MODEL_PATH, LABELS_PATH)
                if META_MODEL_PATH.exists():
                    print(f"🧭 Loading BirdNET meta-model from {META_MODEL_PATH}")
                    meta_model = load_birdnet_model(META_MODEL_PATH, LABELS_PATH)
                else:
                    print(
                        f"⚠️ BirdNET meta-model missing at {META_MODEL_PATH}. Occupancy scores disabled."
                    )

            next_audio = find_next_audio(conn)
            if next_audio is None:
                time.sleep(IDLE_SLEEP_SEC)
                continue

            if not is_file_stable(next_audio):
                print(f"⏳ Skipping active or recently changed file {next_audio}")
                time.sleep(IDLE_SLEEP_SEC)
                continue

            print(f"🎧 Processing {next_audio}")
            process_audio(model, meta_model, conn, next_audio)
            print(f"✅ Finished {next_audio}")
        except Exception as exc:
            if next_audio is not None and next_audio.exists():
                source_key = relative_source(next_audio)
                try:
                    record_failure(conn, source_key, sf.info(next_audio), str(exc))
                except Exception:
                    pass
                delete_source(next_audio)
                try:
                    update_deleted_source(conn, source_key, next_audio)
                except Exception:
                    pass
            print(f"❌ BirdNET processing failure: {exc}", file=sys.stderr)
            time.sleep(ERROR_SLEEP_SEC)


if __name__ == "__main__":
    main()
