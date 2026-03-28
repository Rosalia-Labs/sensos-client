#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

DATA_MOUNT="${SENSOS_DATA_MOUNT:-/sensos/data}"
DATA_SERVICES=(
    "sensos-arecord.service"
    "sensos-compress-audio.service"
    "sensos-read-i2c.service"
    "sensos-birdnet.service"
    "sensos-gps.service"
    "thin-data.service"
)
DATA_TIMERS=(
    "monitor-data-space.timer"
)

data_ops_require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "ERROR: required command not found: $1" >&2
        return 1
    }
}

data_ops_run_systemctl() {
    sudo systemctl "$@"
}

data_ops_unit_exists() {
    local unit_name="$1"
    systemctl list-unit-files --type=service --type=timer 2>/dev/null | awk '{print $1}' | grep -Fxq "${unit_name}"
}

data_ops_stop_unit_if_present() {
    local unit_name="$1"

    if data_ops_unit_exists "${unit_name}"; then
        data_ops_run_systemctl stop "${unit_name}" >/dev/null 2>&1 || true
        echo "Stopped ${unit_name}"
    fi
}

data_ops_start_unit_if_present() {
    local unit_name="$1"

    if data_ops_unit_exists "${unit_name}"; then
        data_ops_run_systemctl start "${unit_name}" >/dev/null 2>&1 || true
        echo "Started ${unit_name}"
    fi
}

data_ops_stop_data_units() {
    local unit_name

    for unit_name in "${DATA_TIMERS[@]}"; do
        data_ops_stop_unit_if_present "${unit_name}"
    done

    for unit_name in "${DATA_SERVICES[@]}"; do
        data_ops_stop_unit_if_present "${unit_name}"
    done
}

data_ops_start_data_units() {
    local unit_name

    for unit_name in "${DATA_SERVICES[@]}"; do
        data_ops_start_unit_if_present "${unit_name}"
    done

    for unit_name in "${DATA_TIMERS[@]}"; do
        data_ops_start_unit_if_present "${unit_name}"
    done
}

data_ops_mount_source_for() {
    local target_path="$1"
    findmnt -n -o SOURCE --target "${target_path}" 2>/dev/null || true
}

data_ops_backing_device_for_source() {
    local source_path="$1"
    local pkname

    [[ -n "${source_path}" ]] || return 0

    if [[ -b "${source_path}" ]]; then
        pkname="$(lsblk -no PKNAME "${source_path}" 2>/dev/null | head -n 1)"
        if [[ -n "${pkname}" ]]; then
            printf '/dev/%s\n' "${pkname}"
            return 0
        fi
    fi

    printf '%s\n' "${source_path}"
}

data_ops_checkpoint_sqlite_db() {
    local db_path="$1"

    [[ -f "${db_path}" ]] || return 0
    sqlite3 "${db_path}" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null
    echo "Checkpointed ${db_path}"
}

data_ops_checkpoint_sqlite_dbs() {
    local db_path

    [[ -d "${DATA_MOUNT}" ]] || return 0

    while IFS= read -r db_path; do
        data_ops_checkpoint_sqlite_db "${db_path}"
    done < <(find "${DATA_MOUNT}" -type f -name '*.db' | sort)
}

data_ops_sync_storage() {
    sync
}

data_ops_try_mount_data() {
    if mountpoint -q "${DATA_MOUNT}"; then
        return 0
    fi

    if sudo mount "${DATA_MOUNT}" >/dev/null 2>&1; then
        echo "Mounted ${DATA_MOUNT}"
        return 0
    fi

    return 1
}
