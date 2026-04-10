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
