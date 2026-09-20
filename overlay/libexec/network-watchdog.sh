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
PING_COUNT=3
PING_TIMEOUT=2
# Fixed, infrastructure-independent reachability anchor. Used only to tell
# "my local uplink is fine, the problem is on the server/campus side" (e.g. a
# power or cooling outage at the server's site) apart from "my own link is
# down" -- the two look identical from a failed tunnel ping alone, but call
# for very different responses.
ANCHOR_IP="${SENSOS_WATCHDOG_ANCHOR_IP:-1.1.1.1}"

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

# Keep PersistentKeepalive converged to whatever this device was actually
# provisioned with (network.conf's WG_KEEPALIVE, written by config-network),
# on every run, not just when the tunnel is already down. This is deliberately
# NOT a fixed value: some fleet devices are on metered/IoT cellular plans
# where keepalive traffic is a real, unwanted cost, so config-network may have
# set this to 0 on purpose. If WG_KEEPALIVE isn't present at all (a device
# provisioned before this field existed), leave PersistentKeepalive alone
# rather than guessing at intent.
ensure_keepalive() {
    local wg_conf_file="${WG_DIR}/${NETWORK_NAME}.conf"
    local keepalive_sec
    keepalive_sec="$(read_network_value WG_KEEPALIVE)"
    [[ -n "${keepalive_sec}" ]] || return 0
    [[ -f "${wg_conf_file}" ]] || return 0

    # Already converged: touch nothing, every cycle, indefinitely. Only the
    # transition (first run, or WG_KEEPALIVE actually changed) does any work.
    grep -Eq "^PersistentKeepalive[[:space:]]*=[[:space:]]*${keepalive_sec}[[:space:]]*$" "${wg_conf_file}" && return 0

    if grep -q '^PersistentKeepalive' "${wg_conf_file}"; then
        sed -i "s/^PersistentKeepalive.*/PersistentKeepalive = ${keepalive_sec}/" "${wg_conf_file}"
    else
        printf 'PersistentKeepalive = %s\n' "${keepalive_sec}" >>"${wg_conf_file}"
    fi
    log "Updated ${wg_conf_file}: PersistentKeepalive -> ${keepalive_sec} (from network.conf WG_KEEPALIVE)."

    if have_command wg && ip link show "${WG_IFACE}" >/dev/null 2>&1; then
        local peer
        peer="$(wg show "${WG_IFACE}" peers 2>/dev/null | head -n1)"
        [[ -n "${peer}" ]] && wg set "${WG_IFACE}" peer "${peer}" persistent-keepalive "${keepalive_sec}" 2>/dev/null || true
    fi
}

tunnel_reachable() {
    ip link show "${WG_IFACE}" >/dev/null 2>&1 || return 1
    ping -I "${WG_IFACE}" -c "${PING_COUNT}" -W "${PING_TIMEOUT}" "${SERVER_WG_IP}" >/dev/null 2>&1
}

# Diagnostic-only probes, run only after the tunnel check has already
# failed. Neither one drives FAIL_COUNT/outage tracking -- they only decide
# which escalation steps are worth trying this cycle.
anchor_reachable() {
    ping -c "${PING_COUNT}" -W "${PING_TIMEOUT}" "${ANCHOR_IP}" >/dev/null 2>&1
}

endpoint_reachable() {
    local endpoint_host
    endpoint_host="$(read_network_value WG_ENDPOINT_IP)"
    [[ -n "${endpoint_host}" ]] || return 1
    ping -c "${PING_COUNT}" -W "${PING_TIMEOUT}" "${endpoint_host}" >/dev/null 2>&1
}

default_route_device() {
    ip route show default 2>/dev/null \
        | awk '/^default/ {for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}'
}

# Read one property of one saved connection. Deliberately uses the
# per-connection form of `nmcli -t -f` (the one config-hotspot already relies
# on in the field) rather than list-mode field selection.
nm_prop() {
    nmcli -t -f "$2" connection show "$1" 2>/dev/null | cut -d: -f2- || true
}

nm_saved_connections() {
    nmcli -t -f NAME connection show 2>/dev/null || true
}

nm_active_connections() {
    nmcli -t -f NAME connection show --active 2>/dev/null || true
}

is_ap_profile() {
    [[ "$(nm_prop "$1" 802-11-wireless.mode)" == "ap" ]]
}

# Find a wifi connection that's actually supposed to be an always-on AP, by
# ACTUAL MODE, not name. config-hotspot's own connection-naming has not been
# perfectly stable across the fleet's history, and a device that never
# successfully ran config-hotspot may still be sitting on sensos-pigen's
# original bootstrap AP under a different name entirely -- this is the same
# discovery technique config-hotspot itself uses to find its own AP
# connection, reused here so nothing in this script depends on a specific
# connection name ever again.
#
# Critically, this only considers AP-mode profiles with autoconnect=yes.
# config-wifi's disable_hotspot_reclaim_on_next_boot() deliberately retires an
# AP profile (autoconnect=no, priority=-999) on single-radio devices when
# that radio is being reclaimed for the client uplink instead -- it leaves
# the profile in place, just parked. Without this filter, this watchdog would
# find that parked profile, see it's inactive, and keep trying to bring it
# back up every cycle -- fighting config-wifi's own decision and potentially
# flipping a single-radio device's only radio back into AP mode, breaking the
# very uplink it's supposed to be carrying.
find_ap_connection() {
    local name
    while IFS= read -r name; do
        [[ -n "${name}" ]] || continue
        is_ap_profile "${name}" || continue
        [[ "$(nm_prop "${name}" connection.autoconnect)" == "yes" ]] || continue
        printf '%s\n' "${name}"
        return 0
    done < <(nm_saved_connections)
    return 0
}

# The permanent local-access AP, if this device has one, is the last line of
# recovery when the uplink is down -- it needs to be checked independently of
# tunnel health, every run, not just assumed to still be there. Not every
# device has one (single-radio units dedicate their only radio to the uplink
# instead) -- the "is one even configured" check below is what makes this
# self-adapt to that without hardcoding radio count.
check_ap() {
    local ap_con active_ap

    ap_con="$(find_ap_connection)"
    [[ -n "${ap_con}" ]] || return 0

    local name
    active_ap=""
    while IFS= read -r name; do
        [[ -n "${name}" ]] || continue
        if is_ap_profile "${name}"; then
            active_ap="${name}"
            break
        fi
    done < <(nm_active_connections)
    [[ -n "${active_ap}" ]] && return 0

    log "Local access point '${ap_con}' is configured but not active; bringing it up."
    report_event ap_down --severity warning \
        --detail "network=${NETWORK_NAME}" --detail "connection=${ap_con}" \
        --dedupe-window 1800 --dedupe-key network
    if nmcli connection up "${ap_con}" >>"${LOG_FILE}" 2>&1; then
        log "Local access point '${ap_con}' restored."
        report_event ap_recovered --severity info \
            --detail "network=${NETWORK_NAME}" --detail "connection=${ap_con}"
    else
        log "Failed to bring '${ap_con}' back up; will retry next cycle."
    fi
}

# Link-layer recovery for the client uplink.
#
# On a headless box NetworkManager gives up on a client Wi-Fi profile after a
# single failed handshake: it assumes the password may be wrong, asks for a
# new one, finds no secret agent, fails the activation with 'no-secrets' and
# stops autoconnecting that profile until something explicitly reactivates
# it. (Seen in the field: "disconnected during association, asking for new
# key" / "no secrets: No agents were available for this request".) With the
# uplink down there is no default route at all, so the default-route based
# escalate_reconnect_link has nothing to act on -- this is the step that
# actually recovers it. It is purely local (no network traffic) and a no-op
# whenever a default route exists.
#
# Only client profiles that are meant to autoconnect are touched (a profile
# deliberately parked by config-wifi/config-hotspot stays parked), Wi-Fi
# profiles are always activated with an explicit ifname taken from the
# profile, and an interface currently hosting the AP is never used.
ensure_uplink() {
    UPLINK_REACTIVATED=0
    [[ -z "$(default_route_device)" ]] || return 0

    local name type prio ifname cur_con active_names tried=0
    local -a candidates=() cmd_args=()

    while IFS= read -r name; do
        [[ -n "${name}" ]] || continue
        [[ "$(nm_prop "${name}" connection.autoconnect)" == "yes" ]] || continue
        type="$(nm_prop "${name}" connection.type)"
        case "${type}" in
            802-11-wireless)
                is_ap_profile "${name}" && continue
                ;;
            gsm)
                ;;
            *)
                continue
                ;;
        esac
        prio="$(nm_prop "${name}" connection.autoconnect-priority)"
        candidates+=("${prio:-0}"$'\t'"${name}"$'\t'"${type}")
    done < <(nm_saved_connections)
    (( ${#candidates[@]} > 0 )) || return 0

    active_names="$(nm_active_connections)"
    while IFS=$'\t' read -r prio name type; do
        (( tried < 2 )) || break
        if grep -Fxq -- "${name}" <<<"${active_names}"; then
            continue
        fi
        ifname=""
        cmd_args=(connection up "${name}")
        if [[ "${type}" == "802-11-wireless" ]]; then
            ifname="$(nm_prop "${name}" connection.interface-name)"
            if [[ -z "${ifname}" ]]; then
                log "Uplink profile '${name}' is not bound to an interface; not activating it (risk of taking the AP radio)."
                continue
            fi
            cur_con="$(nmcli -t -f GENERAL.CONNECTION device show "${ifname}" 2>/dev/null | cut -d: -f2 || true)"
            if [[ -n "${cur_con}" && "${cur_con}" != "--" ]] && is_ap_profile "${cur_con}"; then
                log "Interface ${ifname} hosts the local AP ('${cur_con}'); not activating '${name}' on it."
                continue
            fi
            cmd_args+=(ifname "${ifname}")
        fi
        tried=$(( tried + 1 ))
        log "No default route; reactivating uplink profile '${name}'${ifname:+ on ${ifname}} (NetworkManager may have stopped retrying it)."
        if nmcli -w 30 "${cmd_args[@]}" >>"${LOG_FILE}" 2>&1; then
            log "Uplink profile '${name}' is active again."
            UPLINK_REACTIVATED=1
            report_event uplink_reactivated --severity warning \
                --detail "network=${NETWORK_NAME}" --detail "connection=${name}" \
                --dedupe-window 1800 --dedupe-key network
            return 0
        fi
        log "Could not activate '${name}'; will retry next cycle."
    done < <(printf '%s\n' "${candidates[@]}" | sort -t $'\t' -k1,1nr)
    return 0
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
    if [[ -z "${con}" || "${con}" == "--" ]]; then
        log "No active connection found on default route device ${dev}."
        return
    fi
    # Defense in depth: an AP-mode connection should never own the default
    # route (it runs ipv4.method=shared, no upstream gateway), but never
    # touch the local-access AP here regardless of what it's named -- this
    # step is strictly for the uplink radio, never the AP radio, on any
    # topology or naming history. Checked by actual mode, not name -- see
    # find_ap_connection for why.
    local con_mode
    con_mode="$(nmcli -t -f 802-11-wireless.mode connection show "${con}" 2>/dev/null | cut -d: -f2)"
    if [[ "${con_mode}" == "ap" ]]; then
        log "Default route device ${dev} unexpectedly resolved to an AP-mode connection ('${con}'); not reconnecting it."
        return
    fi
    log "Reconnecting ${dev} (${con}) to refresh the underlying link."
    nmcli connection up "${con}" >>"${LOG_FILE}" 2>&1 || true
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
    check_ap
    ensure_uplink
    if (( UPLINK_REACTIVATED )); then
        # Give the fresh association / DHCP / first WireGuard handshake a
        # moment so a just-recovered link isn't immediately counted as down.
        sleep 10
    fi

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

    # Classify the outage before deciding what to do about it. A failed
    # tunnel ping alone can't tell "my local uplink is down" apart from "the
    # server/campus is unreachable (power/cooling outage, maintenance, etc.)
    # but my link is fine" -- and those call for different responses. Only
    # probe when the tunnel check has already failed, to keep this cheap.
    local local_link_ok=0 endpoint_ok=0 failure_class
    anchor_reachable && local_link_ok=1
    endpoint_reachable && endpoint_ok=1
    if (( local_link_ok )); then
        if (( endpoint_ok )); then
            failure_class="tunnel_only"
        else
            failure_class="server_or_campus_unreachable"
        fi
    else
        failure_class="local_link_down"
    fi

    if [[ -z "${OUTAGE_START}" ]]; then
        OUTAGE_START="$(date -Is)"
        log "Tunnel to ${SERVER_WG_IP} unreachable (check #${FAIL_COUNT}); classified as ${failure_class}."
        report_event network_down --severity warning \
            --detail "network=${NETWORK_NAME}" --detail "class=${failure_class}" \
            --dedupe-window 600 --dedupe-key network
    else
        log "Tunnel to ${SERVER_WG_IP} still unreachable (check #${FAIL_COUNT}); classified as ${failure_class}."
    fi

    # Escalate in stages and keep retrying on the same cadence for as long as
    # the outage lasts, rather than giving up after the first attempt at each
    # stage (assumes the default 10-minute timer cadence): (2 checks ~ 20min)
    # restart the tunnel; then, only if the local link is NOT confirmed
    # healthy: (4 checks ~ 40min) also reconnect the underlying link, (8
    # checks ~ 80min) also bounce NetworkManager entirely. Skipping those last
    # two when the anchor ping succeeds matters in practice: NetworkManager
    # restarts also blip this device's always-on hotspot AP, so there's no
    # reason to keep doing that every 80 minutes through a multi-hour outage
    # that's confirmed to be on the server's end, not ours.
    if (( FAIL_COUNT % 2 == 0 )); then
        escalate_restart_wg
    fi
    if (( local_link_ok )); then
        log "Local uplink confirmed reachable (anchor ${ANCHOR_IP}); skipping link reconnect / NetworkManager restart."
    else
        if (( FAIL_COUNT % 4 == 0 )); then
            escalate_reconnect_link
        fi
        if (( FAIL_COUNT % 8 == 0 )); then
            escalate_restart_networkmanager
            report_event network_down_escalated --severity warning \
                --detail "network=${NETWORK_NAME}" --detail "action=networkmanager_restart" \
                --dedupe-window 1800 --dedupe-key network
        fi
    fi

    write_state
}

main "$@"
