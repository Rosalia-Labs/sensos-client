#!/bin/bash

set -euo pipefail

DEPLOY_ROOT="${SENSOS_CLIENT_ROOT:-/sensos}"
BIRDNET_CONFIG_FILE="${DEPLOY_ROOT}/etc/birdnet.env"
BIRDNET_REQUIREMENTS_FILE="${DEPLOY_ROOT}/etc/birdnet-requirements.txt"
BIRDNET_VENV_DIR="${DEPLOY_ROOT}/python/birdnet-venv"
BIRDNET_STAMP_FILE="${BIRDNET_VENV_DIR}/.requirements.sha256"
BIRDNET_SERVICE="process-birdnet.service"
PYTHON_BIN="${PYTHON_BIN:-python3}"

log() {
    printf '[libexec/setup-birdnet] %s\n' "$*"
}

die() {
    printf '[libexec/setup-birdnet] ERROR: %s\n' "$*" >&2
    exit 1
}

require_root() {
    [[ "${EUID}" -eq 0 ]] || die "run as root"
}

require_inputs() {
    [[ -f "${BIRDNET_REQUIREMENTS_FILE}" ]] || die "missing ${BIRDNET_REQUIREMENTS_FILE}"
}

birdnet_enabled() {
    [[ -f "${BIRDNET_CONFIG_FILE}" ]] || return 1
    grep -Eq '^BIRDNET_ENABLED=1$' "${BIRDNET_CONFIG_FILE}"
}

requirements_declared() {
    grep -Eq '^\s*[^#[:space:]]' "${BIRDNET_REQUIREMENTS_FILE}"
}

requirements_digest() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "${BIRDNET_REQUIREMENTS_FILE}" | awk '{print $1}'
        return
    fi

    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "${BIRDNET_REQUIREMENTS_FILE}" | awk '{print $1}'
        return
    fi

    die "missing sha256sum/shasum; cannot track BirdNET requirements state"
}

ensure_venv() {
    if [[ ! -x "${BIRDNET_VENV_DIR}/bin/python" ]]; then
        log "creating BirdNET virtual environment at ${BIRDNET_VENV_DIR}"
        "${PYTHON_BIN}" -m venv "${BIRDNET_VENV_DIR}"
        BIRDNET_VENV_CREATED=1
    else
        log "reusing existing BirdNET virtual environment at ${BIRDNET_VENV_DIR}"
        BIRDNET_VENV_CREATED=0
    fi
}

install_birdnet_requirements_if_needed() {
    local current_digest
    local previous_digest=""

    current_digest="$(requirements_digest)"
    if [[ -f "${BIRDNET_STAMP_FILE}" ]]; then
        previous_digest="$(head -n 1 "${BIRDNET_STAMP_FILE}" | tr -d '[:space:]')"
    fi

    if [[ "${BIRDNET_VENV_CREATED}" -eq 0 && "${current_digest}" == "${previous_digest}" ]]; then
        log "BirdNET requirements unchanged; skipping pip"
        return
    fi

    if requirements_declared; then
        log "installing BirdNET Python dependencies from ${BIRDNET_REQUIREMENTS_FILE}"
        "${BIRDNET_VENV_DIR}/bin/pip" install -r "${BIRDNET_REQUIREMENTS_FILE}"
    else
        log "no BirdNET Python requirements declared; skipping pip"
    fi

    printf '%s\n' "${current_digest}" >"${BIRDNET_STAMP_FILE}"
}

set_permissions() {
    chown -R sensos-admin:sensos-data "${BIRDNET_VENV_DIR}"
    find "${BIRDNET_VENV_DIR}" -type d -exec chmod 0755 '{}' +
    find "${BIRDNET_VENV_DIR}" -type f -exec chmod 0644 '{}' +
    find "${BIRDNET_VENV_DIR}/bin" -type f -exec chmod 0755 '{}' +
}

reconcile_service_state() {
    if birdnet_enabled; then
        systemctl enable "${BIRDNET_SERVICE}"
        log "enabled ${BIRDNET_SERVICE}"
    else
        systemctl disable --now "${BIRDNET_SERVICE}" >/dev/null 2>&1 || true
        log "BirdNET not enabled; disabled ${BIRDNET_SERVICE}"
    fi
}

main() {
    require_root
    require_inputs

    if ! birdnet_enabled; then
        reconcile_service_state
        return 0
    fi

    ensure_venv
    install_birdnet_requirements_if_needed
    set_permissions
    reconcile_service_state
}

main "$@"
