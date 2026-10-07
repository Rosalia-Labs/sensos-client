#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Reapplies the uplink's tc cap based on time of day. Run periodically
# (sensos-uplink-cap.timer) rather than scheduled exactly at the day/night
# boundary: idempotent re-application tolerates a missed tick, a reboot (tc
# state doesn't survive those), or NetworkManager bringing the interface back
# up after a drop, without needing to hook any of those events directly.
#
# Config lives in uplink-cap.conf (written by config-uplink-cap). Both the
# bandwidth numbers and the day/night boundary are site-specific -- a shared
# field-station uplink's spare capacity and quiet hours aren't something this
# script can know or guess, so there is no built-in default cap here beyond
# "do nothing" when uplink-cap.conf is absent.

set -euo pipefail

resolve_overlay_root() {
    printf '%s\n' "${SENSOS_CLIENT_ROOT:-/sensos}"
}

OVERLAY_ROOT="$(resolve_overlay_root)"
TC_UPLINK_LIB="${OVERLAY_ROOT}/libexec/tc-uplink.sh"
WIFI_CONF="${OVERLAY_ROOT}/etc/wifi.conf"
UPLINK_CAP_CONF="${OVERLAY_ROOT}/etc/uplink-cap.conf"

[[ -f "${TC_UPLINK_LIB}" ]] || {
    echo "Missing tc-uplink library at ${TC_UPLINK_LIB}" >&2
    exit 1
}
# shellcheck disable=SC1090
source "${TC_UPLINK_LIB}"

if [[ ! -f "${UPLINK_CAP_CONF}" ]]; then
    echo "No ${UPLINK_CAP_CONF}; uplink cap scheduling not configured, nothing to do."
    exit 0
fi
# shellcheck disable=SC1090
source "${UPLINK_CAP_CONF}"

iface="${IFACE:-}"
if [[ -z "${iface}" ]]; then
    if [[ -f "${WIFI_CONF}" ]]; then
        # shellcheck disable=SC1090
        source "${WIFI_CONF}"
        iface="${UPLINK_INTERFACE:-}"
    fi
fi
if [[ -z "${iface}" ]]; then
    echo "Could not determine uplink interface (set IFACE in ${UPLINK_CAP_CONF}, or configure ${WIFI_CONF} via config-wifi)." >&2
    exit 1
fi

if ! ip link show "${iface}" >/dev/null 2>&1; then
    echo "Interface ${iface} not present; skipping this run."
    exit 0
fi

in_day_window() {
    local start="$1" end="$2" hour="$3"
    if ((start < end)); then
        ((hour >= start && hour < end))
    else
        # Window wraps past midnight (e.g. a day window of 20-6).
        ((hour >= start || hour < end))
    fi
}

hour="$(date +%-H)"

use_day_cap=0
if [[ -n "${DAY_START_HOUR:-}" && -n "${DAY_END_HOUR:-}" ]]; then
    if in_day_window "${DAY_START_HOUR}" "${DAY_END_HOUR}" "${hour}"; then
        use_day_cap=1
    fi
else
    # No day window configured: treat the whole day as "day" so the
    # configured cap (if any) is always applied, rather than silently
    # going uncapped.
    use_day_cap=1
fi

if [[ "${use_day_cap}" == "1" ]]; then
    up="${DAY_LIMIT_UP_KBIT:-}"
    down="${DAY_LIMIT_DOWN_KBIT:-}"
    echo "Hour ${hour}: day window; up='${up:-none}' down='${down:-none}'."
else
    up="${NIGHT_LIMIT_UP_KBIT:-}"
    down="${NIGHT_LIMIT_DOWN_KBIT:-}"
    echo "Hour ${hour}: night window; up='${up:-none}' down='${down:-none}'."
fi

if [[ -z "${up}" && -z "${down}" ]]; then
    clear_tc_limits "${iface}"
else
    command -v tc >/dev/null 2>&1 || {
        echo "ERROR: missing required command: tc" >&2
        exit 1
    }
    apply_tc_limits "${iface}" "${up}" "${down}"
fi
