#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC
#
# Shared by sensos-client's periodic watchdog scripts (network-watchdog.sh,
# i2c-watchdog.sh, ...). Only genuinely generic helpers belong here -- log to
# a file, report an event if the CLI exists, check a command is present.
# Nothing network- or I2C-specific: a watchdog for one subsystem living
# inside another's script (or its shared lib growing subsystem-specific
# logic) is exactly the naming confusion this file exists to avoid. Callers
# must set LOG_FILE and LOG_DIR before sourcing.

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
