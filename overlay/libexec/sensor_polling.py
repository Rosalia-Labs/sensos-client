#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rosalia Labs LLC

"""Generic numeric-sensor polling: priority-queue scheduling, subsample
averaging with backoff on failure, and flattening into the
(timestamp, device_address, sensor_type, key, value) rows the shared
readings table expects.

Deliberately bus-agnostic. Device discovery and error-recovery are different
enough per transport (I2C's bus-reset-on-OSError has no serial equivalent;
a serial port's reopen-on-SerialException has no I2C equivalent) that they
stay in each transport's own reader (read-i2c-sensors.py, read-teros-sensors.py,
...) rather than being forced into one shared abstraction here. What's
actually identical across transports -- scheduling, averaging, storage -- is
everything in this module.
"""

from __future__ import annotations

import datetime
import heapq
import itertools
import sys
import time
from typing import Callable, Optional

MAX_ATTEMPTS = 3
BACKOFF_MULTIPLIER = 2
MAX_BACKOFF_SEC = 3600


def get_interval(config: dict[str, str], key: str) -> Optional[int]:
    value_str = config.get(key, "").strip()
    if value_str:
        try:
            value = int(value_str)
            return value if value > 0 else None
        except ValueError:
            return None
    fallback_str = config.get("INTERVAL_SEC", "").strip()
    if fallback_str:
        try:
            value = int(fallback_str)
            return value if value > 0 else None
        except ValueError:
            return None
    return None


def get_subsamples_per_interval(config: dict[str, str]) -> int:
    raw = config.get("SUBSAMPLES_PER_INTERVAL", "").strip()
    if not raw:
        return 1
    try:
        value = int(raw)
    except ValueError:
        return 1
    return max(1, value)


def average_sensor_samples(samples: list[dict]) -> Optional[dict]:
    if not samples:
        return None
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for sample in samples:
        if not sample:
            continue
        for key, value in sample.items():
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            sums[key] = sums.get(key, 0.0) + numeric
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    averaged: dict[str, float] = {}
    for key, total in sums.items():
        count = counts.get(key, 0)
        if count <= 0:
            continue
        averaged[key] = round(total / count, 3)
    return averaged or None


def read_with_retries(sensor: dict, max_attempts: int = MAX_ATTEMPTS) -> Optional[dict]:
    """sensor["read_func"] is called as read_func(sensor["addr"]) -- any
    transport-specific retry/reset behavior (e.g. an I2C bus reset) belongs
    inside that callable, not here. This is just the generic attempt loop."""
    for attempt in range(1, max_attempts + 1):
        try:
            data = sensor["read_func"](sensor["addr"])
            if data:
                return data
            print(
                f"{sensor['sensor_type']} returned no data (attempt {attempt}/{max_attempts})"
            )
        except Exception as exc:
            print(
                f"Error on attempt {attempt} reading {sensor['sensor_type']}: {exc}",
                file=sys.stderr,
            )
        time.sleep(0.2)
    return None


def flatten_sensor_data(sensor_data, device_address, sensor_type, timestamp):
    if not sensor_data:
        return []
    flat = []
    for key, value in sensor_data.items():
        try:
            flat.append((timestamp, device_address, sensor_type, key, float(value)))
        except (TypeError, ValueError):
            continue
    return flat


def run_polling_loop(
    sensors: list[dict],
    subsamples_per_interval: int,
    store_readings: Callable[[list[tuple]], None],
) -> None:
    """Runs forever. Each entry in `sensors` needs: key, addr, sensor_type,
    read_func, base_interval. Never returns under normal operation -- callers
    should treat an empty `sensors` list as a startup error before calling
    this, not rely on it returning.

    Queue entries are (next_time, tiebreaker, sensor) rather than plain
    (next_time, sensor): heapq falls back to comparing the second tuple
    element to break a tie on the first, and sensor dicts aren't orderable --
    two entries queued in the same tight loop can easily get the identical
    time.time() value (seen in practice), which would otherwise crash with
    "'<' not supported between instances of 'dict' and 'dict'". The
    tiebreaker is a monotonically increasing counter, so ties always resolve
    on it before ever reaching the dicts."""
    tiebreaker = itertools.count()
    polling_queue: list[tuple[float, int, dict]] = []
    for sensor in sensors:
        sensor["current_interval"] = sensor["base_interval"]
        heapq.heappush(polling_queue, (time.time(), next(tiebreaker), sensor))

    print(f"Using subsamples_per_interval={subsamples_per_interval}")
    print("Entering sensor loop (priority queue with retries + backoff)")
    while polling_queue:
        now = time.time()
        next_time, _, sensor = heapq.heappop(polling_queue)
        wait = max(0, next_time - now)
        if wait:
            time.sleep(wait)

        timestamp = (
            datetime.datetime.now(datetime.UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        print(
            f"Polling {sensor['sensor_type']} at {sensor['addr']} "
            f"with {subsamples_per_interval} subsample(s)..."
        )

        sample_values: list[dict] = []
        subsample_spacing_sec = sensor["base_interval"] / max(subsamples_per_interval, 1)
        for subsample_index in range(subsamples_per_interval):
            sample = read_with_retries(sensor)
            if sample:
                sample_values.append(sample)
            if subsample_index < subsamples_per_interval - 1:
                time.sleep(subsample_spacing_sec)

        averaged_data = average_sensor_samples(sample_values)
        if averaged_data:
            print(
                f"{sensor['sensor_type']} ({sensor['addr']}) averaged "
                f"{len(sample_values)}/{subsamples_per_interval} samples: {averaged_data}"
            )
            readings = flatten_sensor_data(
                averaged_data, sensor["addr"], sensor["sensor_type"], timestamp
            )
            store_readings(readings)
            sensor["current_interval"] = sensor["base_interval"]
        else:
            print(
                f"No valid subsamples were captured for {sensor['sensor_type']} "
                f"at {sensor['addr']}; backing off."
            )
            sensor["current_interval"] = min(
                sensor["current_interval"] * BACKOFF_MULTIPLIER, MAX_BACKOFF_SEC
            )

        next_poll_time = next_time + sensor["current_interval"]
        heapq.heappush(polling_queue, (next_poll_time, next(tiebreaker), sensor))
