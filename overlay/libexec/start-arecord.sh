#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

set -u
umask 0002

SCRIPT_FILE="$(realpath "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_FILE}")" && pwd)"
OVERLAY_ROOT="${SENSOS_CLIENT_ROOT:-/sensos}"
CLIENT_ROOT="${SENSOS_CLIENT_ROOT:-${OVERLAY_ROOT}}"

CONFIG_FILE="${CLIENT_ROOT}/etc/arecord.conf"
LOCATION_FILE="${CLIENT_ROOT}/etc/location.conf"

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

format_coord_token() {
    local value="$1"
    local positive="$2"
    local negative="$3"
    local abs_value scaled

    [[ -n "${value}" ]] || {
        printf '%s\n' "na"
        return 0
    }

    if ! [[ "${value}" =~ ^-?[0-9]+([.][0-9]+)?$ ]]; then
        printf '%s\n' "na"
        return 0
    fi

    abs_value="${value#-}"
    scaled="$(printf '%s' "${abs_value}" | awk '{printf "%07d", int(($1 * 10000) + 0.5)}')"
    if [[ "${value}" == -* ]]; then
        printf '%s%s\n' "${negative}" "${scaled}"
    else
        printf '%s%s\n' "${positive}" "${scaled}"
    fi
}

location_token() {
    local latitude longitude

    [[ -f "${LOCATION_FILE}" ]] || {
        printf '%s\n' ""
        return 0
    }

    latitude="$(awk -F= '/^LATITUDE=/{print $2}' "${LOCATION_FILE}" | tail -n 1 | tr -d '[:space:]')"
    longitude="$(awk -F= '/^LONGITUDE=/{print $2}' "${LOCATION_FILE}" | tail -n 1 | tr -d '[:space:]')"

    if [[ -z "${latitude}" || -z "${longitude}" ]]; then
        printf '%s\n' ""
        return 0
    fi

    printf '_%s_%s\n' \
        "$(format_coord_token "${latitude}" "N" "S")" \
        "$(format_coord_token "${longitude}" "E" "W")"
}

LOCATION_TOKEN="$(location_token)"
OUTPUT_PATTERN="${BASE_DIR}/queued/%Y/%m/%d/sensos_%Y-%m-%dT%H-%M-%SZ${LOCATION_TOKEN}.wav"

permission_diagnostics() {
    local target="$1"

    echo "ERROR: recording path is not writable by $(id -un): ${target}" >&2
    stat -c '  path=%n owner=%U group=%G mode=%A (%a)' "${target}" >&2 2>/dev/null || true
    findmnt -T "${target}" -n -o 'TARGET,SOURCE,FSTYPE,OPTIONS' >&2 2>/dev/null || true
    echo "  identity=$(id)" >&2
}

mkdir -p "$BASE_DIR" "$BASE_DIR/queued" "$BASE_DIR/compressed" "$BASE_DIR/processed" || {
    permission_diagnostics "$BASE_DIR"
    exit 1
}

for runtime_dir in "$BASE_DIR" "$BASE_DIR/queued" "$BASE_DIR/compressed" "$BASE_DIR/processed"; do
    if [[ ! -w "${runtime_dir}" ]]; then
        permission_diagnostics "${runtime_dir}"
        exit 1
    fi
done

ensure_output_dirs() {
    local day_offset queued_dir

    for day_offset in 0 1; do
        queued_dir="${BASE_DIR}/queued/$(date -u -d "+${day_offset} day" +%Y/%m/%d)"
        mkdir -p "${queued_dir}" || {
            permission_diagnostics "$(dirname "${queued_dir}")"
            return 1
        }
    done
}

refresh_output_dirs() {
    while true; do
        ensure_output_dirs
        sleep 60
    done
}

ensure_output_dirs || exit 1
refresh_output_dirs &
DIR_WATCH_PID=$!

cleanup() {
    kill "${DIR_WATCH_PID}" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

echo "Starting continuous recording with the following settings:"
echo "  DEVICE:   $DEVICE"
echo "  FORMAT:   $FORMAT"
echo "  CHANNELS: $CHANNELS"
echo "  RATE:     $RATE"
echo "  MAX_TIME: $MAX_TIME seconds"
echo "  OUTPUT:   $OUTPUT_PATTERN"
echo "Press Ctrl+C to stop."

arecord -D "$DEVICE" \
    -f "$FORMAT" \
    -c "$CHANNELS" \
    -r "$RATE" \
    --max-file-time="$MAX_TIME" \
    --use-strftime "$OUTPUT_PATTERN"

exit $?
