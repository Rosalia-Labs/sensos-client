#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

SCRIPT_FILE="$(realpath "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_FILE}")" && pwd)"
OVERLAY_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CLIENT_ROOT="${SENSOS_CLIENT_ROOT:-$(cd "${OVERLAY_ROOT}/.." && pwd)}"

CONFIG_FILE="${CLIENT_ROOT}/etc/arecord.conf"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: Configuration file not found at $CONFIG_FILE"
    exit 1
fi

source "$CONFIG_FILE"

: "${DEVICE:?Missing DEVICE in config}"
: "${FORMAT:?Missing FORMAT in config}"
: "${CHANNELS:?Missing CHANNELS in config}"
: "${RATE:?Missing RATE in config}"
: "${MAX_TIME:?Missing MAX_TIME in config}"

if [ -z "$BASE_DIR" ]; then
    BASE_DIR="${CLIENT_ROOT}/data/audio_recordings"
fi

OUTPUT_PATTERN="${BASE_DIR}/queued/%Y/%m/%d/sensos_%Y%m%dT%H%M%S.wav"

mkdir -p "$BASE_DIR"
sudo chown -R sensos-admin:sensos-data "$BASE_DIR"
sudo chmod -R 2775 "$BASE_DIR"

echo "Starting continuous recording with the following settings:"
echo "  DEVICE:   $DEVICE"
echo "  FORMAT:   $FORMAT"
echo "  CHANNELS: $CHANNELS"
echo "  RATE:     $RATE"
echo "  MAX_TIME: $MAX_TIME seconds"
echo "  OUTPUT:   $OUTPUT_PATTERN"
echo "Press Ctrl+C to stop."

exec arecord -D "$DEVICE" \
    -f "$FORMAT" \
    -c "$CHANNELS" \
    -r "$RATE" \
    --max-file-time="$MAX_TIME" \
    --use-strftime "$OUTPUT_PATTERN"
