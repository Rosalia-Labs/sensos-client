#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Shared tc helpers for capping an uplink interface. Used both for a
# one-off manual cap (config-wifi --limit-up-kbit/--limit-down-kbit) and for
# the scheduled day/night cap (config-uplink-cap, apply-uplink-cap.sh).

clear_tc_limits() {
    local dev="$1"
    sudo tc qdisc del dev "${dev}" root 2>/dev/null || true
    sudo tc qdisc del dev "${dev}" ingress 2>/dev/null || true
}

apply_tc_limits() {
    local dev="$1"
    local up_kbit="${2:-}"
    local down_kbit="${3:-}"

    if [[ -n "${up_kbit}" ]]; then
        echo "Applying egress cap ${up_kbit} kbit on ${dev}..."
        sudo tc qdisc del dev "${dev}" root 2>/dev/null || true
        sudo tc qdisc add dev "${dev}" root tbf rate "${up_kbit}kbit" burst 32kbit latency 400ms
    fi

    if [[ -n "${down_kbit}" ]]; then
        echo "Applying ingress cap ${down_kbit} kbit on ${dev}..."
        sudo tc qdisc del dev "${dev}" handle ffff: ingress 2>/dev/null || true
        sudo tc qdisc add dev "${dev}" handle ffff: ingress
        sudo tc filter add dev "${dev}" parent ffff: protocol all u32 \
            match u32 0 0 police rate "${down_kbit}kbit" burst 32k drop flowid :1
    fi
}
