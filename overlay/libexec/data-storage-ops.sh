#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

DATA_MOUNT="${SENSOS_DATA_MOUNT:-/sensos/data}"
DATA_SERVICES=(
    "sensos-arecord.service"
    "sensos-compress-audio.service"
    "sensos-read-i2c.service"
    "sensos-upload-i2c.service"
    "sensos-birdnet.service"
    "sensos-gps.service"
    "sensos-thin-data.service"
)
DATA_TIMERS=(
    "sensos-monitor-data-space.timer"
)
DATA_ARCHIVE_MODE_STATE_FILE="${SENSOS_DATA_ARCHIVE_MODE_STATE_FILE:-/sensos/log/archive-mode.state}"
DATA_LAYOUT_TOP_LEVEL_DIRS=(
    "audio_recordings"
    "birdnet"
)
DATA_LAYOUT_AUDIO_SUBDIRS=(
    "queued"
    "compressed"
    "processed"
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

data_ops_unit_is_active() {
    local unit_name="$1"
    data_ops_unit_exists "${unit_name}" && systemctl is-active --quiet "${unit_name}"
}

data_ops_stop_unit_if_present() {
    local unit_name="$1"

    if data_ops_unit_exists "${unit_name}"; then
        echo "Checking ${unit_name}"
        data_ops_run_systemctl stop "${unit_name}" >/dev/null 2>&1 || true
    fi
}

data_ops_start_unit_if_present() {
    local unit_name="$1"

    if data_ops_unit_exists "${unit_name}"; then
        echo "Checking ${unit_name}"
        data_ops_run_systemctl start "${unit_name}" >/dev/null 2>&1 || true
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

data_ops_start_selected_units() {
    local unit_name

    for unit_name in "$@"; do
        data_ops_start_unit_if_present "${unit_name}"
    done
}

data_ops_list_active_units() {
    local unit_name

    for unit_name in "${DATA_SERVICES[@]}"; do
        if data_ops_unit_is_active "${unit_name}"; then
            printf '%s\n' "${unit_name}"
        fi
    done

    for unit_name in "${DATA_TIMERS[@]}"; do
        if data_ops_unit_is_active "${unit_name}"; then
            printf '%s\n' "${unit_name}"
        fi
    done
}

data_ops_any_data_writer_active() {
    local unit_name

    for unit_name in "${DATA_SERVICES[@]}"; do
        if data_ops_unit_is_active "${unit_name}"; then
            return 0
        fi
    done

    return 1
}

data_ops_archive_mode_is_active() {
    sudo test -f "${DATA_ARCHIVE_MODE_STATE_FILE}"
}

data_ops_mount_source_for() {
    local target_path="$1"
    findmnt -n -o SOURCE --target "${target_path}" 2>/dev/null || true
}

data_ops_root_mount_source() {
    findmnt -n -o SOURCE --target / 2>/dev/null || true
}

data_ops_uses_root_filesystem() {
    local data_source root_source

    [[ -d "${DATA_MOUNT}" ]] || return 1

    data_source="$(data_ops_mount_source_for "${DATA_MOUNT}")"
    root_source="$(data_ops_root_mount_source)"

    [[ -n "${data_source}" && -n "${root_source}" && "${data_source}" == "${root_source}" ]]
}

data_ops_storage_is_available() {
    mountpoint -q "${DATA_MOUNT}" || data_ops_uses_root_filesystem
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
    if data_ops_storage_is_available; then
        return 0
    fi

    if sudo mount "${DATA_MOUNT}" >/dev/null 2>&1; then
        echo "Mounted ${DATA_MOUNT}"
        return 0
    fi

    return 1
}

data_ops_reset_data_root() {
    [[ -d "${DATA_MOUNT}" ]] || {
        echo "ERROR: data mount path not found: ${DATA_MOUNT}" >&2
        return 1
    }

    sudo find "${DATA_MOUNT}" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    sudo chown sensos-admin:sensos-data "${DATA_MOUNT}"
    sudo chmod 2775 "${DATA_MOUNT}"
    echo "Cleared ${DATA_MOUNT}"
}

data_ops_has_meaningful_data_content() {
    local entry=""
    local name=""

    [[ -d "${DATA_MOUNT}" ]] || return 1

    while IFS= read -r entry; do
        name="$(basename "${entry}")"
        case "${name}" in
            audio_recordings)
                if sudo find "${entry}" -mindepth 1 -maxdepth 1 ! \( \
                    -type d \( -name queued -o -name compressed -o -name processed \) \
                \) | read -r; then
                    return 0
                fi
                if sudo find "${entry}" \( -path "${entry}/queued" -o -path "${entry}/compressed" -o -path "${entry}/processed" \) \
                    -prune -o -mindepth 2 -print | read -r; then
                    return 0
                fi
                ;;
            birdnet)
                if sudo find "${entry}" -mindepth 1 | read -r; then
                    return 0
                fi
                ;;
            *)
                return 0
                ;;
        esac
    done < <(sudo find "${DATA_MOUNT}" -mindepth 1 -maxdepth 1 -type d | sort)

    if sudo find "${DATA_MOUNT}" -mindepth 1 -maxdepth 1 ! -type d | read -r; then
        return 0
    fi

    return 1
}
