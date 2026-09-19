#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC
#
# Periodically verifies that the WireGuard tunnel back to the SensOS server
# is actually passing traffic. wg-quick@<network>.service is a oneshot unit:
# once it starts, systemd considers it "active" forever, even if the tunnel
# has gone silently dead (e.g. a NAT/carrier-grade-NAT UDP mapping expired
# during an idle period). This script is the thing that actually notices and
# self-heals, escalating from "just nudge the tunnel" to "bounce the
# underlying link" the longer the outage persists.

set -euo pipefail

CLIENT_ROOT="${SENSOS_CLIENT_ROOT:-/sensos}"
NETWORK_CONF="${CLIENT_ROOT}/etc/network.conf"
LOG_DIR="${CLIENT_ROOT}/log"
LOG_FILE="${LOG_DIR}/network-watchdog.log"
STATE_FILE="${LOG_DIR}/network-watchdog.state"
WG_DIR="/etc/wireguard"
KEEPALIVE_SEC="${SENSOS_WG_KEEPALIVE_SEC:-25}"
PING_COUNT=3
PING_TIMEOUT=2

have_command() {
    command -v "$1" >/dev/null 2>&1
}

log() {
    mkdir -p "${LOG_DIR}"
    printf '[%s] %s\n' "$(date -Is)" "$*" | tee -a "${LOG_FILE}"
}

report_event() {
    have_command sensos-report-event || return 0
    sensos-report-event "$@" || true
}

read_network_value() {
    local wanted_key="$1" key value

    [[ -f "${NETWORK_CONF}" ]] || return 0
    while IFS='=' read -r key value; do
        [[ -n "${key}" ]] || continue
        if [[ "${key}" == "${wanted_key}" ]]; then
            printf '%s\n' "${value}"
            return 0
        fi
    done < <(grep -Ev '^\s*(#|$)' "${NETWORK_CONF}" || true)
}

read_state() {
    FAIL_COUNT=0
    OUTAGE_START=""
    if [[ -f "${STATE_FILE}" ]]; then
        # shellcheck disable=SC1090
        source "${STATE_FILE}"
    fi
}

write_state() {
    printf 'FAIL_COUNT=%d\nOUTAGE_START=%q\n' "${FAIL_COUNT}" "${OUTAGE_START}" >"${STATE_FILE}"
}

# Keep PersistentKeepalive converged on every run, not just when the tunnel
# is already down. This is what makes the fix reach devices that were
# provisioned before a nonzero default existed, without needing a full
# reprovision: NAT/carrier-grade-NAT mappings never go stale from client-side
# silence once this is in place.
ensure_keepalive() {
    local wg_conf_file="${WG_DIR}/${NETWORK_NAME}.conf"
    [[ -f "${wg_conf_file}" ]] || return 0

    if ! grep -Eq "^PersistentKeepalive[[:space:]]*=[[:space:]]*${KEEPALIVE_SEC}[[:space:]]*$" "${wg_conf_file}"; then
        if grep -q '^PersistentKeepalive' "${wg_conf_file}"; then
            sed -i "s/^PersistentKeepalive.*/PersistentKeepalive = ${KEEPALIVE_SEC}/" "${wg_conf_file}"
        else
            printf 'PersistentKeepalive = %s\n' "${KEEPALIVE_SEC}" >>"${wg_conf_file}"
        fi
        log "Updated ${wg_conf_file}: PersistentKeepalive -> ${KEEPALIVE_SEC}."
    fi

    if have_command wg && ip link show "${WG_IFACE}" >/dev/null 2>&1; then
        local peer
        peer="$(wg show "${WG_IFACE}" peers 2>/dev/null | head -n1)"
        [[ -n "${peer}" ]] && wg set "${WG_IFACE}" peer "${peer}" persistent-keepalive "${KEEPALIVE_SEC}" 2>/dev/null || true
    fi
}

tunnel_reachable() {
    ip link show "${WG_IFACE}" >/dev/null 2>&1 || return 1
    ping -I "${WG_IFACE}" -c "${PING_COUNT}" -W "${PING_TIMEOUT}" "${SERVER_WG_IP}" >/dev/null 2>&1
}

default_route_device() {
    ip route show default 2>/dev/null \
        | awk '/^default/ {for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}'
}

escalate_restart_wg() {
    log "Restarting ${WG_UNIT} to force a fresh handshake."
    systemctl restart "${WG_UNIT}" >>"${LOG_FILE}" 2>&1 || true
}

escalate_reconnect_link() {
    local dev con

    dev="$(default_route_device)"
    if [[ -z "${dev}" || "${dev}" == "${WG_IFACE}" ]]; then
        log "No non-WireGuard default route device found to reconnect."
        return
    fi
    con="$(nmcli -t -f GENERAL.CONNECTION device show "${dev}" 2>/dev/null | cut -d: -f2)"
    if [[ -n "${con}" && "${con}" != "--" ]]; then
        log "Reconnecting ${dev} (${con}) to refresh the underlying link."
        nmcli connection up "${con}" >>"${LOG_FILE}" 2>&1 || true
    else
        log "No active connection found on default route device ${dev}."
    fi
}

escalate_restart_networkmanager() {
    log "Restarting NetworkManager as a last resort."
    systemctl restart NetworkManager >>"${LOG_FILE}" 2>&1 || true
}

main() {
    mkdir -p "${LOG_DIR}"

    NETWORK_NAME="$(read_network_value NETWORK_NAME)"
    SERVER_WG_IP="$(read_network_value SERVER_WG_IP)"
    if [[ -z "${NETWORK_NAME}" || -z "${SERVER_WG_IP}" ]]; then
        log "NETWORK_NAME or SERVER_WG_IP not set in ${NETWORK_CONF}; nothing to watch yet."
        exit 0
    fi
    WG_IFACE="${NETWORK_NAME}"
    WG_UNIT="wg-quick@${NETWORK_NAME}"

    read_state
    ensure_keepalive

    if tunnel_reachable; then
        if (( FAIL_COUNT > 0 )); then
            local down_for="n/a"
            if [[ -n "${OUTAGE_START}" ]]; then
                down_for="$(( $(date +%s) - $(date -d "${OUTAGE_START}" +%s) ))s"
            fi
            log "Tunnel to ${SERVER_WG_IP} recovered after ${FAIL_COUNT} failed check(s); down for ${down_for}."
            report_event network_recovered --severity info \
                --detail "network=${NETWORK_NAME}" --detail "down_for=${down_for}"
        fi
        FAIL_COUNT=0
        OUTAGE_START=""
        write_state
        exit 0
    fi

    FAIL_COUNT=$(( FAIL_COUNT + 1 ))
    if [[ -z "${OUTAGE_START}" ]]; then
        OUTAGE_START="$(date -Is)"
        log "Tunnel to ${SERVER_WG_IP} unreachable (check #${FAIL_COUNT})."
        report_event network_down --severity warning \
            --detail "network=${NETWORK_NAME}" --dedupe-window 600 --dedupe-key network
    else
        log "Tunnel to ${SERVER_WG_IP} still unreachable (check #${FAIL_COUNT})."
    fi

    # Escalate in stages and keep retrying on the same cadence for as long as
    # the outage lasts, rather than giving up after the first attempt at each
    # stage: (2 checks ~ 4min) restart the tunnel, (4 checks ~ 8min) also
    # reconnect the underlying link, (8 checks ~ 16min) also bounce
    # NetworkManager entirely.
    if (( FAIL_COUNT % 2 == 0 )); then
        escalate_restart_wg
    fi
    if (( FAIL_COUNT % 4 == 0 )); then
        escalate_reconnect_link
    fi
    if (( FAIL_COUNT % 8 == 0 )); then
        escalate_restart_networkmanager
        report_event network_down_escalated --severity warning \
            --detail "network=${NETWORK_NAME}" --detail "action=networkmanager_restart" \
            --dedupe-window 1800 --dedupe-key network
    fi

    write_state
}

main "$@"
