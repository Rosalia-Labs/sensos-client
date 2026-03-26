#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import importlib.util
import os
import sys
import time
from pathlib import Path

import soundfile as sf

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

AUDIO_ROOT = CLIENT_ROOT / "data" / "audio_recordings"
QUEUED_ROOT = AUDIO_ROOT / "queued"
COMPRESSED_ROOT = AUDIO_ROOT / "compressed"
MIN_FILE_AGE_SEC = int(os.environ.get("AUDIO_COMPRESS_MIN_FILE_AGE_SEC", "15"))
FILE_STABLE_SEC = int(os.environ.get("AUDIO_COMPRESS_FILE_STABLE_SEC", "30"))
IDLE_SLEEP_SEC = int(os.environ.get("AUDIO_COMPRESS_IDLE_SLEEP_SEC", "60"))
ERROR_SLEEP_SEC = int(os.environ.get("AUDIO_COMPRESS_ERROR_SLEEP_SEC", "10"))


def ensure_runtime_dirs() -> None:
    create_dir(str(COMPRESSED_ROOT), "sensos-admin", "sensos-data", 0o2775)


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


def find_next_wav() -> Path | None:
    if not QUEUED_ROOT.exists():
        return None

    now = time.time()
    candidates = []
    for path in QUEUED_ROOT.rglob("*.wav"):
        try:
            age = now - path.stat().st_mtime
        except FileNotFoundError:
            continue
        if age >= MIN_FILE_AGE_SEC:
            candidates.append(path)
    return min(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def compressed_path_for(source_path: Path) -> Path:
    relative_path = source_path.relative_to(QUEUED_ROOT)
    return (COMPRESSED_ROOT / relative_path).with_suffix(".flac")


def prune_empty_dirs(start: Path) -> None:
    current = start
    while current != QUEUED_ROOT and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def compress_once(source_path: Path) -> None:
    target_path = compressed_path_for(source_path)
    create_dir(str(target_path.parent), "sensos-admin", "sensos-data", 0o2775)
    audio, sample_rate = sf.read(source_path, dtype="int32", always_2d=True)
    sf.write(target_path, audio, sample_rate, format="FLAC")
    print(f"Compressed {source_path} -> {target_path}")
    source_path.unlink(missing_ok=True)
    prune_empty_dirs(source_path.parent)


def main() -> None:
    setup_logging("compress_queued_audio.log")
    ensure_runtime_dirs()

    while True:
        next_wav = None
        try:
            next_wav = find_next_wav()
            if next_wav is None:
                time.sleep(IDLE_SLEEP_SEC)
                continue

            if not is_file_stable(next_wav):
                print(f"Skipping active or recently changed file {next_wav}")
                time.sleep(IDLE_SLEEP_SEC)
                continue

            compress_once(next_wav)
        except Exception as exc:
            if next_wav is not None:
                print(f"Compression failure for {next_wav}: {exc}", file=sys.stderr)
            else:
                print(f"Compression failure: {exc}", file=sys.stderr)
            time.sleep(ERROR_SLEEP_SEC)


if __name__ == "__main__":
    main()
