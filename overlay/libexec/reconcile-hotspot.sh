#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC
#
# Makes the local-access AP match /sensos/etc/hotspot.conf. The one place that
# writes that intent file and the one place that changes the AP's NetworkManager
# state, so config-hotspot, config-wifi, and network-watchdog never disagree.
#
#   reconcile-hotspot.sh set-intent <true|false> <iface> <connection> <ssid>
#   reconcile-hotspot.sh reconcile [--report]
#
# Idempotent. --report makes the watchdog's ap_down / ap_recovered events
# visible; config-driven runs stay quiet about deliberate state changes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT_ROOT="${SENSOS_CLIENT_ROOT:-/sensos}"
HOTSPOT_CONF="${CLIENT_ROOT}/etc/hotspot.conf"
WIFI_CONF="${CLIENT_ROOT}/etc/wifi.conf"
LOG_DIR="${CLIENT_ROOT}/log"
LOG_FILE="${LOG_DIR}/hotspot.log"

# shellcheck source=./watchdog-common.sh
source "${SCRIPT_DIR}/watchdog-common.sh"

NMCLI=(nmcli)
if (( EUID != 0 )); then
    NMCLI=(sudo nmcli)
fi

read_conf_value() {
    local file="$1" wanted_key="$2" key value

    [[ -f "${file}" ]] || return 0
    while IFS='=' read -r key value; do
        if [[ "${key}" == "${wanted_key}" ]]; then
            value="${value%\"}"
            value="${value#\"}"
            printf '%s\n' "${value}"
            return 0
        fi
    done < <(grep -Ev '^\s*(#|$)' "${file}" || true)
}

nm_prop() {
    nmcli -t -f "$2" connection show "$1" 2>/dev/null | cut -d: -f2- || true
}

profile_exists() {
    nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "$1"
}

profile_active() {
    nmcli -t -f NAME connection show --active 2>/dev/null | grep -Fxq "$1"
}

set_intent() {
    local enabled="$1" iface="$2" connection="$3" ssid="$4"

    mkdir -p "$(dirname "${HOTSPOT_CONF}")"
    cat >"${HOTSPOT_CONF}" <<EOF
# Operator intent for the local access point. The NetworkManager profile holds the
# password; reconcile-hotspot.sh applies this file.
AP_ENABLED=${enabled}
AP_INTERFACE="${iface}"
AP_CONNECTION="${connection}"
AP_SSID="${ssid}"
UPDATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
EOF
    chmod 664 "${HOTSPOT_CONF}"
}

migrate_intent() {
    local candidate enabled=false iface ssid
    for candidate in sensos-ap sensosap; do
        profile_exists "${candidate}" || continue
        [[ "$(nm_prop "${candidate}" 802-11-wireless.mode)" == "ap" ]] || continue
        [[ "$(nm_prop "${candidate}" connection.autoconnect)" == "yes" ]] && enabled=true
        iface="$(nm_prop "${candidate}" connection.interface-name)"
        ssid="$(nm_prop "${candidate}" 802-11-wireless.ssid)"
        set_intent "${enabled}" "${iface}" "${candidate}" "${ssid}"
        log "Recorded AP intent from existing profile '${candidate}' (enabled=${enabled})."
        return 0
    done
    return 1
}

reconcile() {
    local report="$1" enabled iface connection uplink_iface

    if [[ ! -f "${HOTSPOT_CONF}" ]]; then
        migrate_intent || return 0
    fi

    enabled="$(read_conf_value "${HOTSPOT_CONF}" AP_ENABLED)"
    iface="$(read_conf_value "${HOTSPOT_CONF}" AP_INTERFACE)"
    connection="$(read_conf_value "${HOTSPOT_CONF}" AP_CONNECTION)"
    [[ -n "${connection}" ]] || return 0

    if ! profile_exists "${connection}"; then
        log "AP intent names '${connection}' but no such NetworkManager profile exists; leaving it alone."
        return 0
    fi

    uplink_iface="$(read_conf_value "${WIFI_CONF}" UPLINK_INTERFACE)"
    if [[ "${enabled}" == "true" && -n "${uplink_iface}" && "${iface}" == "${uplink_iface}" ]]; then
        log "AP '${connection}' is set to ${iface}, which carries the uplink; keeping it down."
        report_event hotspot_uplink_clash --severity warning \
            --detail "interface=${iface}" --dedupe-window 3600 --dedupe-key hotspot
        enabled=false
    fi

    if [[ "${enabled}" == "true" ]]; then
        "${NMCLI[@]}" connection modify "${connection}" connection.autoconnect yes connection.autoconnect-priority 100 || true
        if ! profile_active "${connection}"; then
            log "Local access point '${connection}' is enabled but not active; bringing it up."
            if [[ "${report}" == "--report" ]]; then
                report_event ap_down --severity warning \
                    --detail "connection=${connection}" --dedupe-window 1800 --dedupe-key hotspot
            fi
            if "${NMCLI[@]}" connection up "${connection}" >>"${LOG_FILE}" 2>&1; then
                log "Local access point '${connection}' restored."
                if [[ "${report}" == "--report" ]]; then
                    report_event ap_recovered --severity info --detail "connection=${connection}"
                fi
            else
                log "Failed to bring '${connection}' up; will retry next run."
            fi
        fi
    else
        "${NMCLI[@]}" connection modify "${connection}" connection.autoconnect no connection.autoconnect-priority -999 || true
        if profile_active "${connection}"; then
            log "Bringing down local access point '${connection}' (intent is disabled)."
            "${NMCLI[@]}" connection down "${connection}" >>"${LOG_FILE}" 2>&1 || true
        fi
    fi
}

retire() {
    local iface="$1" connection ssid

    if [[ ! -f "${HOTSPOT_CONF}" ]]; then
        migrate_intent || true
    fi
    connection="$(read_conf_value "${HOTSPOT_CONF}" AP_CONNECTION)"
    ssid="$(read_conf_value "${HOTSPOT_CONF}" AP_SSID)"
    set_intent false "${iface}" "${connection}" "${ssid}"
    reconcile ""
}

main() {
    mkdir -p "${LOG_DIR}"
    case "${1:-}" in
    set-intent)
        set_intent "$2" "$3" "$4" "$5"
        ;;
    reconcile)
        reconcile "${2:-}"
        ;;
    retire)
        retire "$2"
        ;;
    *)
        echo "usage: $0 set-intent <true|false> <iface> <connection> <ssid> | reconcile [--report] | retire <iface>" >&2
        exit 2
        ;;
    esac
}

main "$@"
