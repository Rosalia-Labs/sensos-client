#!/bin/bash

set -euo pipefail

DEPLOY_ROOT="${SENSOS_CLIENT_ROOT:-/sensos}"
I2C_REQUIREMENTS_FILE="${DEPLOY_ROOT}/etc/i2c-requirements.txt"
VENV_DIR="${DEPLOY_ROOT}/python/venv"
STAMP_FILE="${VENV_DIR}/.i2c-requirements.sha256"
PYTHON_BIN="${VENV_DIR}/bin/python"
LGPIO_AVAILABLE=1

log() {
    printf '[libexec/setup-i2c-python] %s\n' "$*"
}

die() {
    printf '[libexec/setup-i2c-python] ERROR: %s\n' "$*" >&2
    exit 1
}

require_root() {
    [[ "${EUID}" -eq 0 ]] || die "run as root"
}

require_inputs() {
    [[ -f "${I2C_REQUIREMENTS_FILE}" ]] || die "missing ${I2C_REQUIREMENTS_FILE}"
    [[ -x "${PYTHON_BIN}" ]] || die "missing ${PYTHON_BIN}; run ./install first"
}

package_is_available() {
    local package_name="$1"
    apt-cache show --no-all-versions "${package_name}" >/dev/null 2>&1
}

resolve_package_spec() {
    local package_spec="$1"
    local candidate
    local -a package_candidates=()

    IFS='|' read -r -a package_candidates <<< "${package_spec}"

    for candidate in "${package_candidates[@]}"; do
        [[ -n "${candidate}" ]] || continue
        if package_is_available "${candidate}"; then
            printf '%s\n' "${candidate}"
            return
        fi
    done

    die "no installable package found for spec '${package_spec}'"
}

resolve_optional_package_spec() {
    local package_spec="$1"
    local candidate
    local -a package_candidates=()

    IFS='|' read -r -a package_candidates <<< "${package_spec}"
    for candidate in "${package_candidates[@]}"; do
        [[ -n "${candidate}" ]] || continue
        if package_is_available "${candidate}"; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done

    return 1
}

requirements_declared() {
    grep -Eq '^\s*[^#[:space:]]' "${I2C_REQUIREMENTS_FILE}"
}

requirements_digest() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "${I2C_REQUIREMENTS_FILE}" | awk '{print $1}'
        return
    fi

    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "${I2C_REQUIREMENTS_FILE}" | awk '{print $1}'
        return
    fi

    die "missing sha256sum/shasum; cannot track I2C Python requirements state"
}

required_system_dev_package() {
    python3 -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}-dev")'
}

python_headers_available() {
    python3 -c 'import pathlib, sysconfig; include_dir = sysconfig.get_config_var("INCLUDEPY") or ""; print(int(pathlib.Path(include_dir, "Python.h").is_file()))' | grep -qx '1'
}

ensure_build_support() {
    local lgpio_package
    local dev_package

    if lgpio_package="$(resolve_optional_package_spec "liblgpio-dev|lgpio")"; then
        log "installing ${lgpio_package} for I2C GPIO access"
        apt-get install -y "${lgpio_package}"
    else
        log "no installable lgpio apt package found; continuing without lgpio support on this host"
        LGPIO_AVAILABLE=0
        return
    fi

    if ! command -v swig >/dev/null 2>&1; then
        log "installing swig for lgpio wheel build"
        apt-get install -y swig
    fi

    if python_headers_available; then
        return
    fi

    dev_package="$(required_system_dev_package)"
    log "installing ${dev_package} for Python C extension builds"
    apt-get install -y "${dev_package}"
    python_headers_available || die "Python headers are still unavailable after installing ${dev_package}"
}

install_requirements_if_needed() {
    local current_digest
    local previous_digest=""
    local requirements_file="${I2C_REQUIREMENTS_FILE}"
    local temp_requirements_file=""

    current_digest="$(requirements_digest)"
    if [[ -f "${STAMP_FILE}" ]]; then
        previous_digest="$(head -n 1 "${STAMP_FILE}" | tr -d '[:space:]')"
    fi

    if [[ "${current_digest}" == "${previous_digest}" ]]; then
        log "I2C Python requirements unchanged; skipping pip"
        return
    fi

    if requirements_declared; then
        if [[ "${LGPIO_AVAILABLE}" -eq 0 ]]; then
            temp_requirements_file="$(mktemp "${VENV_DIR}/i2c-requirements.no-lgpio.XXXXXX.txt")"
            grep -Ev '^\s*(lgpio)\s*($|[<>=!~])' "${I2C_REQUIREMENTS_FILE}" > "${temp_requirements_file}" || true
            requirements_file="${temp_requirements_file}"
            log "installing I2C Python dependencies without lgpio on this host"
        else
            log "installing I2C Python dependencies from ${I2C_REQUIREMENTS_FILE}"
        fi
        "${PYTHON_BIN}" -m pip install -r "${requirements_file}"
    else
        log "no I2C Python requirements declared; skipping pip"
    fi

    if [[ -n "${temp_requirements_file}" && -f "${temp_requirements_file}" ]]; then
        rm -f "${temp_requirements_file}"
    fi

    printf '%s\n' "${current_digest}" >"${STAMP_FILE}"
}

main() {
    require_root
    require_inputs
    ensure_build_support
    install_requirements_if_needed
}

main "$@"
