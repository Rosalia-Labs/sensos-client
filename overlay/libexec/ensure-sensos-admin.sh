#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

sensos_admin_should_skip_reexec_for_help() {
    [[ $# -gt 0 ]] || return 1

    local arg
    for arg in "$@"; do
        case "${arg}" in
            -h|--help|help)
                ;;
            *)
                return 1
                ;;
        esac
    done

    return 0
}

# Records that a config-* script actually changed state on this unit (wrote
# a config file, applied a profile, enabled/disabled a service -- not just
# viewed --help/--list/--status). Call this explicitly at each script's real
# mutation point, not from ensure_sensos_admin_user itself: that gate runs on
# *every* invocation, including read-only ones, so recording there would
# conflate "ran this script" with "changed something". Mirrors
# record_config_change() in utils.py (the Python equivalent).
record_config_change() {
    local script_name
    script_name="$(basename "$1")"
    local dir="${CLIENT_ROOT:-/sensos}/etc/config-invocations"
    mkdir -p "${dir}"
    date -u +%Y-%m-%dT%H:%M:%SZ >"${dir}/${script_name}"
}

ensure_sensos_admin_user() {
    local script_path="$1"
    shift

    if [[ "${EUID}" -eq 0 ]]; then
        return 0
    fi

    if [[ "$(id -un)" == "sensos-admin" ]]; then
        return 0
    fi

    if sensos_admin_should_skip_reexec_for_help "$@"; then
        return 0
    fi

    if [[ "${SENSOS_ADMIN_REEXEC:-0}" == "1" ]]; then
        echo "ERROR: failed to re-run ${script_path} as sensos-admin." >&2
        exit 1
    fi

    echo "Re-running as sensos-admin..." >&2
    exec sudo --preserve-env=SENSOS_CLIENT_ROOT,SENSOS_ADMIN_REEXEC -u sensos-admin \
        env SENSOS_ADMIN_REEXEC=1 \
        "${script_path}" "$@"
}
