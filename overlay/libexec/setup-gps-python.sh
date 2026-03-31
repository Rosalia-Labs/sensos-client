#!/bin/bash

set -euo pipefail

DEPLOY_ROOT="${SENSOS_CLIENT_ROOT:-/sensos}"
GPS_REQUIREMENTS_FILE="${DEPLOY_ROOT}/etc/gps-requirements.txt"
VENV_DIR="${DEPLOY_ROOT}/python/venv"
STAMP_FILE="${VENV_DIR}/.gps-requirements.sha256"
PYTHON_BIN="${VENV_DIR}/bin/python"

log() {
    printf '[libexec/setup-gps-python] %s\n' "$*"
}

die() {
    printf '[libexec/setup-gps-python] ERROR: %s\n' "$*" >&2
    exit 1
}

require_root() {
    [[ "${EUID}" -eq 0 ]] || die "run as root"
}

require_inputs() {
    [[ -f "${GPS_REQUIREMENTS_FILE}" ]] || die "missing ${GPS_REQUIREMENTS_FILE}"
    [[ -x "${PYTHON_BIN}" ]] || die "missing ${PYTHON_BIN}; run ./install first"
}

requirements_declared() {
    grep -Eq '^\s*[^#[:space:]]' "${GPS_REQUIREMENTS_FILE}"
}

requirements_digest() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "${GPS_REQUIREMENTS_FILE}" | awk '{print $1}'
        return
    fi

    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "${GPS_REQUIREMENTS_FILE}" | awk '{print $1}'
        return
    fi

    die "missing sha256sum/shasum; cannot track GPS Python requirements state"
}

install_requirements_if_needed() {
    local current_digest
    local previous_digest=""

    current_digest="$(requirements_digest)"
    if [[ -f "${STAMP_FILE}" ]]; then
        previous_digest="$(head -n 1 "${STAMP_FILE}" | tr -d '[:space:]')"
    fi

    if [[ "${current_digest}" == "${previous_digest}" ]]; then
        log "GPS Python requirements unchanged; skipping pip"
        return
    fi

    if requirements_declared; then
        log "installing GPS Python dependencies from ${GPS_REQUIREMENTS_FILE}"
        "${PYTHON_BIN}" -m pip install -r "${GPS_REQUIREMENTS_FILE}"
    else
        log "no GPS Python requirements declared; skipping pip"
    fi

    printf '%s\n' "${current_digest}" >"${STAMP_FILE}"
}

main() {
    require_root
    require_inputs
    install_requirements_if_needed
}

main "$@"
