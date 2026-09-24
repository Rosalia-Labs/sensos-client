#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC
#
# I2C host self-heal.
#
# On a device with I2C sensors configured, /dev/i2c-1 must exist because
# dtparam=i2c_arm=on was applied in /boot/firmware/config.txt at boot -- see
# ensure-i2c-host.sh. Nothing else re-checks this after initial setup, so if
# that line is ever lost (seen in the field, 2026-09: an unclean power-off on
# a power-unreliable site corrupting the FAT32 boot partition, not something
# an I2C sensor merely being unplugged would do -- an unplugged sensor leaves
# /dev/i2c-1 itself alone and just fails individual reads, which
# read-i2c-sensors.py already tolerates), I2C silently stays dead until
# someone physically finds the box and runs raspi-config by hand.
#
# This re-applies the host config (safe/idempotent even when nothing was
# actually wrong) and reboots to make it take effect -- dtparam only applies
# at boot, so there is no way to bring I2C back without one. That is judged
# safe here in a way a reboot-on-network-failure is not (see
# network-watchdog.sh's "no reboot" policy on its escalation steps): by the
# time this runs, the device has already booted cleanly and has a network
# link, so the reboot applies a fix already known to work rather than
# gambling on recovery of a live fault. Rebooted at most once per outage: if
# I2C is still down on the next check after that, physical inspection is
# needed (disconnected HAT, hardware fault) and a repeated reboot would not
# help.
#
# Deliberately its own script/timer, not folded into network-watchdog.sh --
# I2C host health has nothing to do with networking, and a script living
# inside another's namesake is exactly the kind of naming confusion this
# fleet's tooling should avoid (see [[sensos-client-microenv-dir-naming]]).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT_ROOT="${SENSOS_CLIENT_ROOT:-/sensos}"
LOG_DIR="${CLIENT_ROOT}/log"
LOG_FILE="${LOG_DIR}/i2c-watchdog.log"
STATE_FILE="${LOG_DIR}/i2c-host.state"

# shellcheck source=./watchdog-common.sh
source "${SCRIPT_DIR}/watchdog-common.sh"

is_raspberry_pi_host() {
    local model_file="/proc/device-tree/model"
    [[ -r "${model_file}" ]] || return 1
    tr -d '\0' <"${model_file}" | grep -qi 'raspberry pi'
}

i2c_host_expected() {
    is_raspberry_pi_host || return 1
    [[ -f "${CLIENT_ROOT}/etc/i2c-sensors.conf" ]] || return 1
    have_command systemctl || return 1
    systemctl is-enabled --quiet sensos-read-i2c.service 2>/dev/null
}

check_i2c_host() {
    i2c_host_expected || return 0

    local I2C_DOWN_SINCE="" I2C_REBOOTED_AT=""
    [[ -f "${STATE_FILE}" ]] && source "${STATE_FILE}"

    if [[ -e /dev/i2c-1 ]]; then
        if [[ -n "${I2C_DOWN_SINCE}" ]]; then
            log "I2C host recovered (was down since ${I2C_DOWN_SINCE})."
            report_event i2c_host_recovered --severity info
        fi
        rm -f "${STATE_FILE}"
        return 0
    fi

    local now
    now="$(date -Is)"
    [[ -n "${I2C_DOWN_SINCE}" ]] || I2C_DOWN_SINCE="${now}"

    log "/dev/i2c-1 missing; re-applying I2C host configuration."
    report_event i2c_host_down --severity warning \
        --detail "down_since=${I2C_DOWN_SINCE}" \
        --dedupe-window 1800

    "${CLIENT_ROOT}/libexec/ensure-i2c-host.sh" >>"${LOG_FILE}" 2>&1 || true

    if [[ -z "${I2C_REBOOTED_AT}" ]]; then
        log "Rebooting to apply repaired I2C host configuration (dtparam only takes effect at boot)."
        report_event i2c_host_reboot --severity warning \
            --detail "down_since=${I2C_DOWN_SINCE}" \
            --dedupe-window 21600
        printf 'I2C_DOWN_SINCE=%q\nI2C_REBOOTED_AT=%q\n' "${I2C_DOWN_SINCE}" "${now}" >"${STATE_FILE}"
        systemctl reboot
        exit 0
    fi

    log "/dev/i2c-1 still missing after a reboot already attempted at ${I2C_REBOOTED_AT}; not rebooting again. Likely needs physical inspection (disconnected sensors/HAT, hardware fault)."
    report_event i2c_host_down_persistent --severity warning \
        --detail "down_since=${I2C_DOWN_SINCE}" --detail "rebooted_at=${I2C_REBOOTED_AT}" \
        --dedupe-window 21600
    printf 'I2C_DOWN_SINCE=%q\nI2C_REBOOTED_AT=%q\n' "${I2C_DOWN_SINCE}" "${I2C_REBOOTED_AT}" >"${STATE_FILE}"
}

main() {
    mkdir -p "${LOG_DIR}"
    check_i2c_host
}

main "$@"
