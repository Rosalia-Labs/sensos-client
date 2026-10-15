#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC
#
# Shared by scripts that append lines to boot/system config files
# (ensure-i2c-host.sh, setup/06-system-config). ensure_line_present() writes
# via a temp file in the same directory, fsync, then an atomic rename --
# rather than a plain `>>` append -- so an unclean power-off mid-write can't
# corrupt the target file. Field devices on unreliable power lose lines to a
# plain append exactly the way this fleet lost `dtparam=i2c_arm=on` from
# /boot/firmware/config.txt (2026-09): that file is FAT32, which isn't
# crash-safe, and the append itself was never fsynced, so a power cut during
# or shortly after it could leave the file truncated back to a stale state on
# the next fsck. Sourcing scripts must define log() before sourcing this file.

file_contains_line() {
    local file_path="$1"
    local wanted_line="$2"

    [[ -f "${file_path}" ]] || return 1
    grep -Fxq "${wanted_line}" "${file_path}"
}

ensure_line_present() {
    local file_path="$1"
    local wanted_line="$2"
    local dir_path tmp_file

    # Only create the file (and parent dirs) if it's missing -- `install`
    # copies its source's content to the destination unconditionally, so
    # calling this on a file that already exists would silently truncate
    # it to empty (the /dev/null source) before the check below ever runs,
    # discarding every line any earlier call (or anything else) had
    # written. Confirmed as a real bug, not a theoretical one: callers that
    # ensure multiple different lines in the same file (e.g.
    # configure_pi_hardware's i2c/w1-gpio/spi entries in /etc/modules and
    # config.txt) would end up with only the last line surviving.
    [[ -f "${file_path}" ]] || install -D -m 0644 /dev/null "${file_path}"
    file_contains_line "${file_path}" "${wanted_line}" && return 0

    dir_path="$(dirname "${file_path}")"
    tmp_file="$(mktemp "${dir_path}/.$(basename "${file_path}").XXXXXX")"
    cp -p "${file_path}" "${tmp_file}"
    printf '%s\n' "${wanted_line}" >>"${tmp_file}"
    # `sync FILE` (GNU coreutils) fsyncs the file itself before it's visible
    # under its final name; harmless (falls back to a plain `sync`) if that
    # form isn't supported.
    sync "${tmp_file}" 2>/dev/null || true
    mv -f "${tmp_file}" "${file_path}"
    sync "${dir_path}" 2>/dev/null || sync
    log "added '${wanted_line}' to ${file_path}"
}
