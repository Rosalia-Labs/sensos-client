#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import importlib.util
import math
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
from birdnet_data import ensure_schema, get_or_create_source_file, mark_source_status

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
ensure_runtime_dir = UTILS_MODULE.ensure_runtime_dir

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
    mode = config.get("BIRDNET_INPUT_MODE", "split-channels")
    if mode not in {"mono", "split-channels"}:
        raise RuntimeError(f"Unsupported BIRDNET_INPUT_MODE='{mode}' in {config_path}")
    return mode


def read_add_beamformed_channel(config_path: Path) -> bool:
    config = read_birdnet_config(config_path)
    return config.get("BIRDNET_ADD_BEAMFORMED_CHANNEL", "0").strip() == "1"


def read_mic_distances_m(config_path: Path) -> list[float]:
    config = read_birdnet_config(config_path)
    raw = config.get("BIRDNET_MIC_DISTANCES_M", "").strip()
    if not raw:
        return []
    try:
        return [float(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError:
        print(
            f"WARNING: invalid BIRDNET_MIC_DISTANCES_M='{raw}' in {config_path}; ignoring.",
            file=sys.stderr,
        )
        return []


def read_min_threshold(config_path: Path, key: str) -> float:
    config = read_birdnet_config(config_path)
    raw_value = config.get(key, "0")
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid {key}='{raw_value}' in {config_path}") from exc
    if not 0.0 <= value <= 1.0:
        raise RuntimeError(f"Invalid {key}='{raw_value}' in {config_path}; expected 0..1")
    return value


BACKEND_PREFERENCE = read_backend_preference(BIRDNET_CONFIG)
INPUT_MODE = read_input_mode(BIRDNET_CONFIG)
ADD_BEAMFORMED_CHANNEL = read_add_beamformed_channel(BIRDNET_CONFIG)
MIC_DISTANCES_M = read_mic_distances_m(BIRDNET_CONFIG)
SPEED_OF_SOUND_MPS = 343.0
MIN_SCORE = read_min_threshold(BIRDNET_CONFIG, "BIRDNET_MIN_SCORE")
MIN_LIKELIHOOD = read_min_threshold(BIRDNET_CONFIG, "BIRDNET_MIN_LIKELIHOOD")
MIN_VOLUME = read_min_threshold(BIRDNET_CONFIG, "BIRDNET_MIN_VOLUME")
MIN_SCORE_X_LIKELIHOOD = read_min_threshold(
    BIRDNET_CONFIG, "BIRDNET_MIN_SCORE_X_LIKELIHOOD"
)
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
    human_label_mask: np.ndarray


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
    weighted_label: str
    weighted_score: float
    weighted_likely_score: float | None
    human_vocal_score: float

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
        human_label_mask=np.array([is_human_label(label) for label in labels], dtype=bool),
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


def invoke_birdnet_top_labels(
    audio: np.ndarray,
    model: BirdNETModel,
    meta_model: BirdNETModel | None,
    latitude: float | None,
    longitude: float | None,
    observed_on: date,
) -> tuple[str, float, float | None, str, float, float | None, float]:
    input_data = np.expand_dims(audio, axis=0).astype(np.float32)
    model.interpreter.set_tensor(model.input_details[0]["index"], input_data)
    model.interpreter.invoke()
    scores = model.interpreter.get_tensor(model.output_details[0]["index"])
    scores_flat = flat_sigmoid(scores.flatten())
    raw_top_index = int(np.argmax(scores_flat))
    # Raw per-class score, not normalized against the rest of the label
    # space -- BirdNET's output is independent per-class sigmoids (no
    # softmax), so there's no well-defined "rest" to compare against
    # without real calibration data. Carried through purely as information
    # (see is_human_label/write_detection_clips for the existing top-label
    # suppression this is deliberately NOT replacing): a bird detection can
    # still win the window even when there's audible human speech
    # underneath it, and this is how much of that leaked in.
    human_vocal_score = (
        float(np.max(scores_flat[model.human_label_mask]))
        if model.human_label_mask.any()
        else 0.0
    )
    likely_scores = None
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
    weighting_scores = likely_scores if likely_scores is not None else np.ones_like(scores_flat)
    weighted_top_index = int(np.argmax(scores_flat * weighting_scores))
    raw_likely_score = (
        float(likely_scores[raw_top_index]) if likely_scores is not None else None
    )
    weighted_likely_score = (
        float(likely_scores[weighted_top_index])
        if likely_scores is not None
        else None
    )
    return (
        model.labels[raw_top_index],
        float(scores_flat[raw_top_index]),
        raw_likely_score,
        model.labels[weighted_top_index],
        float(scores_flat[weighted_top_index]),
        weighted_likely_score,
        human_vocal_score,
    )


def ensure_runtime_dirs() -> None:
    ensure_runtime_dir(STATE_ROOT)
    ensure_runtime_dir(OUTPUT_ROOT)


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
    ensure_schema(conn)
    ensure_state_file_permissions()
    return conn


def was_processed_successfully(conn: sqlite3.Connection, path: Path) -> bool:
    row = conn.execute(
        """
        SELECT birdnet_processed
        FROM source_files
        WHERE source_path = ?
        """,
        (relative_source(path),),
    ).fetchone()
    if row is None:
        return False
    return bool(row[0])


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


def _gcc_phat_delay(signal: np.ndarray, reference: np.ndarray, max_shift: int) -> int:
    """Estimates the integer-sample delay of `signal` relative to
    `reference` via GCC-PHAT (Generalized Cross-Correlation with Phase
    Transform): cross-correlate in the frequency domain, then whiten by
    dividing out the magnitude so phase (timing) alone drives the
    correlation peak, rather than whichever channel happens to be louder.
    Standard technique for time-delay-of-arrival estimation between
    microphones. A positive return means `signal` lags `reference` (arrived
    later); shifting it backward by that many samples lines it up."""
    fft_size = 1
    while fft_size < len(signal) + len(reference):
        fft_size *= 2

    sig_fft = np.fft.rfft(signal.astype(np.float64), n=fft_size)
    ref_fft = np.fft.rfft(reference.astype(np.float64), n=fft_size)
    cross_spectrum = sig_fft * np.conj(ref_fft)
    magnitude = np.abs(cross_spectrum)
    magnitude[magnitude < 1e-12] = 1e-12
    whitened = cross_spectrum / magnitude

    correlation = np.fft.irfft(whitened, n=fft_size)
    # correlation[0] is zero lag; the tail end of the array represents
    # negative lags, so wrap it around to sit contiguously before zero.
    correlation = np.concatenate((correlation[-max_shift:], correlation[: max_shift + 1]))
    return int(np.argmax(correlation)) - max_shift


def resolve_max_shifts(num_channels: int, max_delay_ms: float) -> list[int]:
    """One alignment-search bound per non-reference channel. Uses measured
    mic-to-channel-0 distances (BIRDNET_MIC_DISTANCES_M) when there's exactly
    one per non-reference channel, converting distance to the physically
    possible maximum delay (distance / speed of sound); otherwise falls back
    to a generic bound derived from max_delay_ms for every channel. A
    mismatched count (geometry measured for the wrong number of channels) is
    treated as absent rather than guessed at, with a warning."""
    generic_shift = max(1, int(max_delay_ms / 1000 * SAMPLE_RATE))
    num_non_reference = num_channels - 1

    if len(MIC_DISTANCES_M) == num_non_reference:
        return [
            max(1, math.ceil(distance / SPEED_OF_SOUND_MPS * SAMPLE_RATE))
            for distance in MIC_DISTANCES_M
        ]

    if MIC_DISTANCES_M:
        print(
            f"WARNING: BIRDNET_MIC_DISTANCES_M has {len(MIC_DISTANCES_M)} value(s) "
            f"but this recording has {num_non_reference} non-reference channel(s); "
            "ignoring and using the generic alignment bound instead.",
            file=sys.stderr,
        )

    return [generic_shift] * num_non_reference


def compute_beamformed_window(
    channels: list[np.ndarray],
    start: int,
    end: int,
    max_shifts: list[int],
) -> np.ndarray:
    """Builds one beamformed 3-second window: every real channel time-aligned
    to channels[0] via GCC-PHAT estimated from THIS window alone, then
    averaged. Delay is re-estimated per window, not once per file -- a fixed
    whole-file delay is only correct for a source that stays in one place,
    and different calls in the same recording routinely come from different
    directions, which need different alignment.

    max_shifts has one bound per non-reference channel (parallel to
    channels[1:]), in samples -- either derived from measured mic geometry
    (distance to channel 0 / speed of sound, tighter and less prone to
    locking onto a spurious correlation peak) or a generic fallback bound
    when geometry isn't known. See resolve_max_shifts.

    Shifted samples are pulled from each channel's full timeline (not just
    the window slice) so a window near a shift boundary still gets real
    audio instead of zero-padding; only windows within max_shift samples of
    the very start or end of the file fall back to zero-padding there."""
    window_len = end - start
    reference = channels[0][start:end]

    aligned = [reference.astype(np.float32)]
    for channel, max_shift in zip(channels[1:], max_shifts):
        delay = _gcc_phat_delay(channel[start:end], reference, max_shift)
        src_start, src_end = start + delay, end + delay
        if src_start >= 0 and src_end <= len(channel):
            aligned.append(channel[src_start:src_end].astype(np.float32))
        else:
            shifted = np.zeros(window_len, dtype=np.float32)
            clipped_start = max(src_start, 0)
            clipped_end = min(src_end, len(channel))
            if clipped_end > clipped_start:
                shifted[clipped_start - src_start : clipped_end - src_start] = channel[
                    clipped_start:clipped_end
                ]
            aligned.append(shifted)

    return np.mean(np.stack(aligned), axis=0).astype(np.float32)


def audio_channels(audio: np.ndarray, input_mode: str) -> list[tuple[int, np.ndarray]]:
    if audio.ndim == 1:
        return [(0, audio.astype(np.float32))]
    if input_mode == "split-channels":
        return [(idx, audio[:, idx].astype(np.float32)) for idx in range(audio.shape[1])]
    return [(0, to_mono(audio))]


def passes_detection_filters(detection: Detection) -> bool:
    if detection.score < MIN_SCORE:
        return False
    if detection.volume < MIN_VOLUME:
        return False
    if MIN_LIKELIHOOD > 0 and (
        detection.likely_score is None or detection.likely_score < MIN_LIKELIHOOD
    ):
        return False
    # Checks weighted_score * weighted_likely_score, not score * likely_score:
    # weighted_label was itself chosen by maximizing exactly that product over
    # every candidate species, so it's always >= the raw label's product.
    # Checking the raw product instead would be a strictly harsher (and
    # mismatched-to-the-name) filter than intended.
    if MIN_SCORE_X_LIKELIHOOD > 0 and (
        detection.weighted_likely_score is None
        or detection.weighted_score * detection.weighted_likely_score < MIN_SCORE_X_LIKELIHOOD
    ):
        return False
    return True


def _classify_window(
    channel_index: int,
    window_index: int,
    start: int,
    end: int,
    inference_audio: np.ndarray,
    volume: float,
    model: BirdNETModel,
    meta_model: BirdNETModel | None,
    latitude: float | None,
    longitude: float | None,
    observed_on: date,
) -> Detection:
    """Shared by collect_detections and collect_beamformed_detections: runs
    BirdNET on one already-assembled WINDOW_FRAMES-length window and builds
    its Detection. inference_audio and volume are separate parameters (not
    derived from each other here) because the short-file caller needs
    inference on zero-padded audio but volume computed on only the real
    samples -- padding would dilute it."""
    (
        label,
        score,
        likely_score,
        weighted_label,
        weighted_score,
        weighted_likely_score,
        human_vocal_score,
    ) = invoke_birdnet_top_labels(
        scale_by_max_value(inference_audio),
        model,
        meta_model,
        latitude,
        longitude,
        observed_on,
    )
    return Detection(
        channel_index,
        window_index,
        start,
        end,
        start,
        volume,
        label,
        score,
        likely_score,
        weighted_label,
        weighted_score,
        weighted_likely_score,
        human_vocal_score,
    )


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
    if frames < WINDOW_FRAMES:
        padded = np.zeros(WINDOW_FRAMES, dtype=np.float32)
        padded[:frames] = audio_mono[:frames]
        volume = normalized_volume(audio_mono[:frames])
        return [
            _classify_window(
                channel_index, 0, 0, WINDOW_FRAMES, padded, volume,
                model, meta_model, latitude, longitude, observed_on,
            )
        ]

    detections: List[Detection] = []
    window_index = 0
    for start in range(0, frames - WINDOW_FRAMES + 1, STRIDE_FRAMES):
        end = start + WINDOW_FRAMES
        window_audio = audio_mono[start:end]
        detections.append(
            _classify_window(
                channel_index, window_index, start, end, window_audio,
                normalized_volume(window_audio),
                model, meta_model, latitude, longitude, observed_on,
            )
        )
        window_index += 1
    return detections


def collect_beamformed_detections(
    channels: list[np.ndarray],
    frames: int,
    channel_index: int,
    model: BirdNETModel,
    meta_model: BirdNETModel | None,
    latitude: float | None,
    longitude: float | None,
    observed_on: date,
    max_delay_ms: float = 50.0,
) -> List[Detection]:
    """Same sliding-window structure as collect_detections, but each
    window's audio is freshly beamformed from all real channels (see
    compute_beamformed_window) instead of sliced from one precomputed
    channel array -- alignment has to be re-estimated per window, not once
    for the whole file, since different calls in the same recording can
    come from different directions."""
    max_shifts = resolve_max_shifts(len(channels), max_delay_ms)

    if frames < WINDOW_FRAMES:
        short_window = compute_beamformed_window(channels, 0, frames, max_shifts)
        padded = np.zeros(WINDOW_FRAMES, dtype=np.float32)
        padded[:frames] = short_window
        volume = normalized_volume(short_window)
        return [
            _classify_window(
                channel_index, 0, 0, WINDOW_FRAMES, padded, volume,
                model, meta_model, latitude, longitude, observed_on,
            )
        ]

    detections: List[Detection] = []
    window_index = 0
    for start in range(0, frames - WINDOW_FRAMES + 1, STRIDE_FRAMES):
        end = start + WINDOW_FRAMES
        window_audio = compute_beamformed_window(channels, start, end, max_shifts)
        detections.append(
            _classify_window(
                channel_index, window_index, start, end, window_audio,
                normalized_volume(window_audio),
                model, meta_model, latitude, longitude, observed_on,
            )
        )
        window_index += 1
    return detections


def merge_consecutive_detections(detections: List[Detection]) -> List[Detection]:
    """Collapse consecutive windows that share the same raw top label into a
    single detection spanning the whole run. Grouped and scored on the raw
    label/score, never the weighted ones -- weighted_score collapses toward
    zero for non-species labels (e.g. "Engine"), which would make it useless
    for picking a run's peak window."""
    merged: List[Detection] = []
    run: List[Detection] = []

    def flush() -> None:
        if not run:
            return
        peak = max(run, key=lambda d: d.score)
        merged.append(
            Detection(
                channel_index=peak.channel_index,
                window_index=run[0].window_index,
                start_frame=run[0].start_frame,
                end_frame=run[-1].end_frame,
                max_score_start_frame=peak.start_frame,
                volume=peak.volume,
                label=peak.label,
                score=peak.score,
                likely_score=peak.likely_score,
                weighted_label=peak.weighted_label,
                weighted_score=peak.weighted_score,
                weighted_likely_score=peak.weighted_likely_score,
                # Unlike the other fields, this deliberately isn't the peak
                # window's value: human speech leaking into a bird detection
                # can land on any window in the run, not necessarily the one
                # with the loudest/most-confident bird score, and the whole
                # point is to not miss that.
                human_vocal_score=max(d.human_vocal_score for d in run),
            )
        )

    for detection in detections:
        if run and detection.label == run[-1].label:
            run.append(detection)
        else:
            flush()
            run = [detection]
    flush()
    return merged


def dedupe_overlapping_channel_detections(detections: List[Detection]) -> List[Detection]:
    """Collapses same-label detections that overlap in time across different
    channels down to one: an overlapping, same-label run on another channel
    is almost always the same physical sound picked up by a second
    microphone, not a separate event, so keeping all of them is mostly
    redundant clips and redundant upload/storage for one real event. Keeps
    the highest-scoring (raw score, never weighted -- see
    merge_consecutive_detections for why) run per overlapping group, ties
    broken by the longer of the tied runs; the rest are dropped before a
    clip is ever written for them.

    Implemented as a sort-and-sweep per label rather than an explicit overlap
    graph: for one-dimensional time intervals the two are equivalent, and
    sort-and-sweep needs no graph/union-find machinery. All channels in one
    source file share the same frame clock, so start_frame/end_frame are
    directly comparable across channels with no conversion.

    A genuine two-individuals-calling-at-once case of the same species from
    different directions is an accepted, rare loss here -- one of the two
    gets dropped, same as any other overlap."""
    by_label: dict[str, list[Detection]] = {}
    for detection in detections:
        by_label.setdefault(detection.label, []).append(detection)

    kept: List[Detection] = []
    for runs in by_label.values():
        runs.sort(key=lambda d: d.start_frame)
        group: list[Detection] = []
        group_end = -1
        for run in runs:
            if group and run.start_frame > group_end:
                kept.append(max(group, key=lambda d: (d.score, d.end_frame - d.start_frame)))
                group = []
            group.append(run)
            group_end = max(group_end, run.end_frame)
        if group:
            kept.append(max(group, key=lambda d: (d.score, d.end_frame - d.start_frame)))

    return kept


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
        ensure_runtime_dir(out_dir)
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
        if len(chunk) < WINDOW_FRAMES:
            padded = np.zeros(WINDOW_FRAMES, dtype=chunk.dtype)
            padded[: len(chunk)] = chunk
            chunk = padded
        sf.write(clip_path, chunk, sample_rate, format="FLAC")
        try:
            clip_path.chmod(0o664)
        except PermissionError:
            pass
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
    source_file_id = get_or_create_source_file(conn, source_key)
    conn.execute(
        "UPDATE source_files SET source_deleted = 0 WHERE id = ?",
        (source_file_id,),
    )
    source_start_dt = require_source_start_datetime(source_path)
    info = sf.info(source_path)
    if info.samplerate != SAMPLE_RATE:
        raise ValueError(
            f"Unsupported sample rate {info.samplerate} for {source_key}; expected {SAMPLE_RATE}"
        )

    conn.execute("DELETE FROM detections WHERE source_file_id = ?", (source_file_id,))
    conn.commit()

    audio, sample_rate = sf.read(source_path, dtype="int32", always_2d=True)
    latitude, longitude = location_coordinates()
    observed_on = source_observation_date(source_path)
    detections: List[Detection] = []
    channel_list = audio_channels(audio, INPUT_MODE)
    for channel_index, channel_audio in channel_list:
        channel_detections = merge_consecutive_detections(
            collect_detections(
                channel_index,
                channel_audio,
                len(channel_audio),
                model,
                meta_model,
                latitude,
                longitude,
                observed_on,
            )
        )
        detections.extend(
            detection
            for detection in channel_detections
            if passes_detection_filters(detection)
        )

    if ADD_BEAMFORMED_CHANNEL and INPUT_MODE == "split-channels" and len(channel_list) >= 2:
        real_channels = [channel_audio for _idx, channel_audio in channel_list]
        beamformed_detections = merge_consecutive_detections(
            collect_beamformed_detections(
                real_channels,
                len(real_channels[0]),
                len(channel_list),
                model,
                meta_model,
                latitude,
                longitude,
                observed_on,
            )
        )
        detections.extend(
            detection
            for detection in beamformed_detections
            if passes_detection_filters(detection)
        )

    detections = dedupe_overlapping_channel_detections(detections)
    written_clips = write_detection_clips(source_path, audio, sample_rate, detections)

    conn.executemany(
        """
        INSERT INTO detections (
            source_file_id, channel_index, window_index, max_score_start_frame, label, score, likely_score, weighted_label, weighted_score, weighted_likely_score, human_vocal_score, volume, clip_start_time, clip_end_time, clip_path, clip_size_bytes, deleted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                source_file_id,
                d.channel_index,
                d.window_index,
                max(0, d.max_score_start_frame - d.start_frame),
                d.label,
                d.score,
                d.likely_score,
                d.weighted_label,
                d.weighted_score,
                d.weighted_likely_score,
                d.human_vocal_score,
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
    mark_source_status(conn, source_key, birdnet_processed=True)
    conn.commit()

    delete_source(source_path)
    mark_source_status(conn, source_key, source_deleted=not source_path.exists())
    conn.commit()


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
                print(
                    f"⚠️ Failed to process {next_audio}: {exc}. Deleting source file.",
                    file=sys.stderr,
                )
                delete_source(next_audio)
                mark_source_status(
                    conn,
                    source_key,
                    birdnet_processed=True,
                    source_deleted=not next_audio.exists(),
                )
                conn.commit()
            print(f"❌ BirdNET processing failure: {exc}", file=sys.stderr)
            time.sleep(ERROR_SLEEP_SEC)


if __name__ == "__main__":
    main()
