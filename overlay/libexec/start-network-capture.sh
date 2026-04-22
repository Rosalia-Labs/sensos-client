#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

set -euo pipefail
umask 0002

CAPTURE_ROOT="${SENSOS_NETWORK_CAPTURE_ROOT:-}"
PCAP_DIR="${CAPTURE_ROOT}/pcap"
INTERFACE_NAME="${SENSOS_NETWORK_CAPTURE_IFACE:-any}"
SNAPLEN_BYTES="${SENSOS_NETWORK_CAPTURE_SNAPLEN:-128}"
ROTATE_FILE_MB="${SENSOS_NETWORK_CAPTURE_FILE_MB:-8}"
ROTATE_FILE_COUNT="${SENSOS_NETWORK_CAPTURE_FILE_COUNT:-48}"
BUFFER_KIB="${SENSOS_NETWORK_CAPTURE_BUFFER_KIB:-4096}"

log() {
    printf '[network-capture] %s\n' "$*"
}

die() {
    printf '[network-capture] ERROR: %s\n' "$*" >&2
    exit 1
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

require_positive_int() {
    local value="$1"
    local name="$2"

    [[ "${value}" =~ ^[0-9]+$ ]] || die "${name} must be a positive integer, got '${value}'"
    (( value > 0 )) || die "${name} must be greater than zero"
}

main() {
    require_cmd tcpdump
    [[ -n "${CAPTURE_ROOT}" ]] || die "SENSOS_NETWORK_CAPTURE_ROOT must be set"
    require_positive_int "${SNAPLEN_BYTES}" "SENSOS_NETWORK_CAPTURE_SNAPLEN"
    require_positive_int "${ROTATE_FILE_MB}" "SENSOS_NETWORK_CAPTURE_FILE_MB"
    require_positive_int "${ROTATE_FILE_COUNT}" "SENSOS_NETWORK_CAPTURE_FILE_COUNT"
    require_positive_int "${BUFFER_KIB}" "SENSOS_NETWORK_CAPTURE_BUFFER_KIB"

    install -d -m 2775 "${CAPTURE_ROOT}"
    install -d -m 2775 "${PCAP_DIR}"

    log "starting tcpdump on interface ${INTERFACE_NAME}; snaplen=${SNAPLEN_BYTES}B rotation=${ROTATE_FILE_COUNT}x${ROTATE_FILE_MB}MB root=${CAPTURE_ROOT}"

    exec tcpdump \
        -i "${INTERFACE_NAME}" \
        -nn \
        -p \
        -U \
        -Z root \
        -B "${BUFFER_KIB}" \
        -s "${SNAPLEN_BYTES}" \
        -y LINUX_SLL \
        -C "${ROTATE_FILE_MB}" \
        -W "${ROTATE_FILE_COUNT}" \
        -w "${PCAP_DIR}/capture.pcap"
}

main "$@"
