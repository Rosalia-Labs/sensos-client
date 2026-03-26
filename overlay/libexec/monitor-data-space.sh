#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

set -euo pipefail

DATA_ROOT="${SENSOS_DATA_ROOT:-/sensos/data}"
STOP_FREE_PERCENT="${SENSOS_STOP_FREE_PERCENT:-5}"
START_FREE_PERCENT="${SENSOS_START_FREE_PERCENT:-15}"
LOGGER_TAG="data-space-monitor"

SERVICES=(
    "sensos-arecord.service"
    "sensos-read-i2c.service"
)

log() {
    logger -t "${LOGGER_TAG}" "$*"
}

die() {
    log "ERROR: $*"
    exit 1
}

require_path() {
    [[ -d "${DATA_ROOT}" ]] || die "data root not found: ${DATA_ROOT}"
}

require_systemctl() {
    command -v systemctl >/dev/null 2>&1 || die "systemctl not found"
}

current_free_percent() {
    df -P "${DATA_ROOT}" | awk 'NR==2 {gsub(/%/, "", $5); print 100 - $5}'
}

unit_exists() {
    local unit_name="$1"
    systemctl list-unit-files --type=service 2>/dev/null | awk '{print $1}' | grep -Fxq "${unit_name}"
}

stop_service_if_needed() {
    local unit_name="$1"

    if ! unit_exists "${unit_name}"; then
        return 0
    fi

    if systemctl is-active --quiet "${unit_name}"; then
        systemctl stop "${unit_name}"
        log "stopped ${unit_name}: free space at ${FREE_PERCENT}% in ${DATA_ROOT}"
    fi
}

start_service_if_needed() {
    local unit_name="$1"

    if ! unit_exists "${unit_name}"; then
        return 0
    fi

    if ! systemctl is-active --quiet "${unit_name}"; then
        systemctl restart "${unit_name}"
        log "started ${unit_name}: free space recovered to ${FREE_PERCENT}% in ${DATA_ROOT}"
    fi
}

main() {
    local unit_name

    require_path
    require_systemctl

    FREE_PERCENT="$(current_free_percent)"
    [[ -n "${FREE_PERCENT}" ]] || die "unable to determine free space for ${DATA_ROOT}"

    if (( FREE_PERCENT <= STOP_FREE_PERCENT )); then
        for unit_name in "${SERVICES[@]}"; do
            stop_service_if_needed "${unit_name}"
        done
        exit 0
    fi

    if (( FREE_PERCENT >= START_FREE_PERCENT )); then
        for unit_name in "${SERVICES[@]}"; do
            start_service_if_needed "${unit_name}"
        done
    fi
}

main "$@"
