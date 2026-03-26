#!/bin/bash

set -euo pipefail

DEPLOY_ROOT="${SENSOS_CLIENT_ROOT:-/sensos}"
BIRDNET_CONFIG_FILE="${DEPLOY_ROOT}/etc/birdnet.env"
BIRDNET_REQUIREMENTS_FILE="${DEPLOY_ROOT}/etc/birdnet-requirements.txt"
BIRDNET_VENV_DIR="${DEPLOY_ROOT}/python/birdnet-venv"
BIRDNET_STAMP_FILE="${BIRDNET_VENV_DIR}/.requirements.sha256"
BIRDNET_SERVICE="sensos-birdnet.service"
THIN_DATA_SERVICE="thin-data.service"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DEFAULT_BIRDNET_BACKEND="litert"

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

birdnet_backend() {
    local backend

    backend="$(awk -F= '/^BIRDNET_BACKEND=/{print $2}' "${BIRDNET_CONFIG_FILE}" 2>/dev/null | tail -n 1 | tr -d '[:space:]')"
    if [[ -z "${backend}" ]]; then
        backend="${DEFAULT_BIRDNET_BACKEND}"
    fi

    case "${backend}" in
        tensorflow)
            printf '%s\n' "${backend}"
            ;;
        litert|tflite)
            printf '%s\n' "litert"
            ;;
        *)
            die "unsupported BIRDNET_BACKEND='${backend}' in ${BIRDNET_CONFIG_FILE}"
            ;;
    esac
}

requirements_declared() {
    grep -Eq '^\s*[^#[:space:]]' "${BIRDNET_REQUIREMENTS_FILE}"
}

requirements_digest() {
    local backend="$1"

    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s:%s\n' "$(sha256sum "${BIRDNET_REQUIREMENTS_FILE}" | awk '{print $1}')" "${backend}"
        return
    fi

    if command -v shasum >/dev/null 2>&1; then
        printf '%s:%s\n' "$(shasum -a 256 "${BIRDNET_REQUIREMENTS_FILE}" | awk '{print $1}')" "${backend}"
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
    local backend="$1"
    local current_digest
    local previous_digest=""
    local backend_package
    local other_package

    case "${backend}" in
        tensorflow)
            backend_package="tensorflow"
            other_package="ai-edge-litert"
            ;;
        litert)
            backend_package="ai-edge-litert"
            other_package="tensorflow"
            ;;
    esac

    current_digest="$(requirements_digest "${backend}")"
    if [[ -f "${BIRDNET_STAMP_FILE}" ]]; then
        previous_digest="$(head -n 1 "${BIRDNET_STAMP_FILE}" | tr -d '[:space:]')"
    fi

    if [[ "${BIRDNET_VENV_CREATED}" -eq 0 && "${current_digest}" == "${previous_digest}" ]]; then
        log "BirdNET requirements unchanged; skipping pip"
        return
    fi

    if requirements_declared; then
        log "installing BirdNET Python dependencies from ${BIRDNET_REQUIREMENTS_FILE} with backend ${backend}"
        "${BIRDNET_VENV_DIR}/bin/pip" install -r "${BIRDNET_REQUIREMENTS_FILE}" "${backend_package}"
    else
        log "no BirdNET Python requirements declared; installing backend ${backend} only"
        "${BIRDNET_VENV_DIR}/bin/pip" install "${backend_package}"
    fi

    "${BIRDNET_VENV_DIR}/bin/pip" uninstall -y "${other_package}" >/dev/null 2>&1 || true

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
        systemctl enable "${THIN_DATA_SERVICE}"
        log "enabled ${BIRDNET_SERVICE}"
        log "enabled ${THIN_DATA_SERVICE}"
    else
        systemctl disable --now "${BIRDNET_SERVICE}" >/dev/null 2>&1 || true
        systemctl disable --now "${THIN_DATA_SERVICE}" >/dev/null 2>&1 || true
        log "BirdNET not enabled; disabled ${BIRDNET_SERVICE}"
        log "BirdNET not enabled; disabled ${THIN_DATA_SERVICE}"
    fi
}

main() {
    local backend

    require_root
    require_inputs

    if ! birdnet_enabled; then
        reconcile_service_state
        return 0
    fi

    backend="$(birdnet_backend)"
    ensure_venv
    install_birdnet_requirements_if_needed "${backend}"
    set_permissions
    reconcile_service_state
}

main "$@"
