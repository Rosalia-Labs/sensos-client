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
    max_score_start_frame: int
    volume: float
    label: str
    score: float
    likely_score: float | None

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


def normalized_volume(audio: np.ndarray) -> float:
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
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            channel_index INTEGER NOT NULL DEFAULT 0,
            window_index INTEGER NOT NULL,
            max_score_start_frame INTEGER NOT NULL,
            label TEXT NOT NULL,
            score REAL NOT NULL,
            likely_score REAL,
            volume REAL,
            clip_start_time TEXT NOT NULL,
            clip_end_time TEXT NOT NULL,
            clip_path TEXT,
            clip_size_bytes INTEGER,
            deleted_at TEXT,
            UNIQUE (source_path, channel_index, window_index)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_detections_source ON detections (source_path, window_index)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_detections_clip_time ON detections (clip_start_time, channel_index)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_detections_clip ON detections (deleted_at, clip_path)"
    )
    conn.commit()
    ensure_state_file_permissions()
    return conn


def was_processed_successfully(conn: sqlite3.Connection, path: Path) -> bool:
    row = conn.execute("SELECT 1 FROM detections WHERE source_path = ?", (relative_source(path),)).fetchone()
    return row is not None


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


def filename_time_token(source_path: Path, detection: Detection, sample_rate: int) -> str:
    start_dt = source_start_datetime(source_path)
    if start_dt is None:
        return source_path.stem
    run_dt = start_dt + timedelta(seconds=(detection.start_frame / sample_rate))
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
        volume = normalized_volume(audio_mono[:frames])
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
                0,
                volume,
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
                start,
                normalized_volume(window_audio),
                label,
                score,
                likely_score,
            )
        )
        window_index += 1
    return detections


def merge_detections(detections: List[Detection]) -> List[Detection]:
    if not detections:
        return []

    merged: List[Detection] = []
    current = Detection(
        channel_index=detections[0].channel_index,
        window_index=detections[0].window_index,
        start_frame=detections[0].start_frame,
        end_frame=detections[0].end_frame,
        max_score_start_frame=detections[0].max_score_start_frame,
        volume=detections[0].volume,
        label=detections[0].label,
        score=detections[0].score,
        likely_score=detections[0].likely_score,
    )

    for detection in detections[1:]:
        same_label = detection.label == current.label
        overlaps = detection.start_frame <= current.end_frame
        if same_label and overlaps:
            current.end_frame = max(current.end_frame, detection.end_frame)
            if detection.score > current.score:
                current.score = detection.score
                current.max_score_start_frame = (
                    detection.max_score_start_frame
                )
            current.volume = max(current.volume, detection.volume)
            if detection.likely_score is not None:
                if current.likely_score is None:
                    current.likely_score = detection.likely_score
                else:
                    current.likely_score = max(current.likely_score, detection.likely_score)
            continue

        merged.append(current)
        current = Detection(
            channel_index=detection.channel_index,
            window_index=detection.window_index,
            start_frame=detection.start_frame,
            end_frame=detection.end_frame,
            max_score_start_frame=detection.max_score_start_frame,
            volume=detection.volume,
            label=detection.label,
            score=detection.score,
            likely_score=detection.likely_score,
        )

    merged.append(current)
    return merged


def write_detection_clips(
    source_path: Path,
    audio: np.ndarray,
    sample_rate: int,
    detections: List[Detection],
) -> dict[tuple[int, int], tuple[Path, int]]:
    written = {}
    loc_token = location_token()
    for detection in detections:
        if is_human_label(detection.label):
            continue
        out_dir = label_output_dir(source_path, detection.label)
        create_dir(str(out_dir), "sensos-admin", "sensos-data", 0o2775)
        start_sec = detection.start_frame / sample_rate
        end_sec = detection.end_frame / sample_rate
        filename = (
            f"{filename_time_token(source_path, detection, sample_rate)}_"
            f"{loc_token}_"
            f"ch{detection.channel_index:02d}_"
            f"{detection.window_index:03d}_"
            f"{sanitize_label(detection.label)}_"
            f"{format_score_token(detection.score, 's')}_"
            f"{format_score_token(detection.likely_score, 'o')}_"
            f"{start_sec:09.3f}-{end_sec:09.3f}.flac"
        )
        clip_path = out_dir / filename
        if audio.ndim == 1:
            chunk = audio[detection.start_frame : detection.end_frame]
        else:
            chunk = audio[detection.start_frame : detection.end_frame, detection.channel_index]
        sf.write(clip_path, chunk, sample_rate, format="FLAC")
        written[(detection.channel_index, detection.window_index)] = (
            clip_path,
            int(clip_path.stat().st_size),
        )
    return written


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
    source_start_dt = require_source_start_datetime(source_path)
    info = sf.info(source_path)
    if info.samplerate != SAMPLE_RATE:
        raise ValueError(
            f"Unsupported sample rate {info.samplerate} for {source_key}; expected {SAMPLE_RATE}"
        )

    conn.execute("DELETE FROM detections WHERE source_path = ?", (source_key,))
    conn.commit()

    audio, sample_rate = sf.read(source_path, dtype="int32", always_2d=True)
    latitude, longitude = location_coordinates()
    detections: List[Detection] = []
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
        detections.extend(merge_detections(channel_detections))
    written_clips = write_detection_clips(source_path, audio, sample_rate, detections)

    conn.executemany(
        """
        INSERT INTO detections (
            source_path, channel_index, window_index, max_score_start_frame, label, score, likely_score, volume, clip_start_time, clip_end_time, clip_path, clip_size_bytes, deleted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                source_key,
                d.channel_index,
                d.window_index,
                d.max_score_start_frame,
                d.label,
                d.score,
                d.likely_score,
                d.volume,
                iso_utc_text(source_start_dt + timedelta(seconds=(d.start_frame / sample_rate))),
                iso_utc_text(source_start_dt + timedelta(seconds=(d.end_frame / sample_rate))),
                (
                    written_clips[(d.channel_index, d.window_index)][0].relative_to(INPUT_ROOT.parent).as_posix()
                    if (d.channel_index, d.window_index) in written_clips
                    else None
                ),
                (
                    written_clips[(d.channel_index, d.window_index)][1]
                    if (d.channel_index, d.window_index) in written_clips
                    else None
                ),
                None,
            )
            for d in detections
        ],
    )
    conn.commit()

    delete_source(source_path)


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
                print(
                    f"⚠️ Failed to process {next_audio}: {exc}. Deleting source file.",
                    file=sys.stderr,
                )
                delete_source(next_audio)
            print(f"❌ BirdNET processing failure: {exc}", file=sys.stderr)
            time.sleep(ERROR_SLEEP_SEC)


if __name__ == "__main__":
    main()
