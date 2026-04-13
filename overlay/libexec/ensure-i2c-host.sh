#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

set -euo pipefail

BOOT_CONFIG_FILE="/boot/firmware/config.txt"
MODULES_FILE="/etc/modules"

log() {
    printf '[ensure-i2c-host] %s\n' "$*"
}

file_contains_line() {
    local file_path="$1"
    local wanted_line="$2"

    [[ -f "${file_path}" ]] || return 1
    grep -Fxq "${wanted_line}" "${file_path}"
}

ensure_line_present() {
    local file_path="$1"
    local wanted_line="$2"

    install -D -m 0644 /dev/null "${file_path}"
    if ! file_contains_line "${file_path}" "${wanted_line}"; then
        printf '%s\n' "${wanted_line}" >>"${file_path}"
        log "added '${wanted_line}' to ${file_path}"
    fi
}

module_loaded() {
    local module_name="$1"

    lsmod | awk '{print $1}' | grep -Fxq "${module_name}"
}

load_module_if_needed() {
    local module_name="$1"

    if module_loaded "${module_name}"; then
        log "kernel module already loaded: ${module_name}"
        return 0
    fi

    if ! command -v modprobe >/dev/null 2>&1; then
        log "modprobe not found; cannot load ${module_name} immediately"
        return 1
    fi

    if modprobe "${module_name}"; then
        log "loaded kernel module ${module_name}"
        return 0
    fi

    log "unable to load kernel module ${module_name} immediately"
    return 1
}

is_raspberry_pi_host() {
    local model_file="/proc/device-tree/model"

    [[ -r "${model_file}" ]] || return 1
    tr -d '\0' <"${model_file}" | grep -qi 'raspberry pi'
}

main() {
    local reboot_required=0

    [[ "${EUID}" -eq 0 ]] || {
        echo "[ensure-i2c-host] ERROR: run as root" >&2
        exit 1
    }

    if ! is_raspberry_pi_host; then
        log "host does not appear to be Raspberry Pi hardware; skipping I2C host configuration"
        return 0
    fi

    if command -v raspi-config >/dev/null 2>&1; then
        if raspi-config nonint do_i2c 0; then
            log "enabled Raspberry Pi I2C support with raspi-config"
        else
            reboot_required=1
            log "raspi-config could not fully enable Raspberry Pi I2C support"
        fi
    fi

    ensure_line_present "${MODULES_FILE}" "i2c_bcm2835"
    ensure_line_present "${BOOT_CONFIG_FILE}" "dtparam=i2c_arm=on"
    ensure_line_present "${MODULES_FILE}" "i2c-dev"

    load_module_if_needed "i2c-dev" || reboot_required=1
    load_module_if_needed "i2c_bcm2835" || reboot_required=1

    if [[ ! -e /dev/i2c-1 ]]; then
        reboot_required=1
        log "I2C device /dev/i2c-1 is not present after configuration"
    fi

    if [[ "${reboot_required}" == "1" ]]; then
        log "I2C host configuration was applied, but a reboot may still be required"
    else
        log "I2C host configuration is active"
    fi
}

main "$@"
