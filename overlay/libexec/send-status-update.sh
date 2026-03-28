#!/bin/bash

set -euo pipefail

CONFIG_FILE="/sensos/etc/network.conf"
API_PATH="/client-status"
API_USER="sensos"
API_PASS_FILE="/sensos/keys/api_password"
VERSION_FILE="/sensos/VERSION"

if [[ ! -f "${VERSION_FILE}" ]]; then
    echo "[ERROR] ${VERSION_FILE} not found." >&2
    exit 1
fi

VERSION="$(head -n 1 "${VERSION_FILE}" | tr -d '[:space:]')"
if [[ -z "${VERSION}" ]]; then
    echo "[ERROR] ${VERSION_FILE} is empty." >&2
    exit 1
fi

if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "[ERROR] ${CONFIG_FILE} not found." >&2
    exit 1
fi

declare -A CFG=()
while IFS='=' read -r key value; do
    [[ -n "${key}" ]] || continue
    CFG["${key}"]="${value}"
done <"${CONFIG_FILE}"

SERVER_WG_IP="${CFG[SERVER_WG_IP]:-}"
SERVER_PORT="${CFG[SERVER_PORT]:-}"
WIREGUARD_IP="${CFG[CLIENT_WG_IP]:-}"

if [[ -z "${SERVER_WG_IP}" || -z "${SERVER_PORT}" || -z "${WIREGUARD_IP}" ]]; then
    echo "[ERROR] SERVER_WG_IP, SERVER_PORT, or CLIENT_WG_IP missing in ${CONFIG_FILE}." >&2
    exit 1
fi

if [[ ! -f "${API_PASS_FILE}" ]]; then
    echo "[ERROR] API password file ${API_PASS_FILE} not found." >&2
    exit 1
fi

API_PASS="$(<"${API_PASS_FILE}")"
API_URL="http://${SERVER_WG_IP}:${SERVER_PORT}${API_PATH}"

hostname="$(hostname)"
uptime_seconds="$(awk '{print int($1)}' /proc/uptime)"
disk_available_gb="$(df --output=avail -BG / | tail -1 | tr -dc '0-9')"
mem_total_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
mem_available_kb="$(awk '/MemAvailable/ {print $2}' /proc/meminfo)"
mem_total_mb="$((mem_total_kb / 1024))"
mem_used_mb="$(((mem_total_kb - mem_available_kb) / 1024))"
read -r load_1m load_5m load_15m _ </proc/loadavg

json_payload="$(
    jq -n \
        --arg hostname "${hostname}" \
        --argjson uptime_seconds "${uptime_seconds}" \
        --argjson disk_available_gb "${disk_available_gb}" \
        --argjson memory_used_mb "${mem_used_mb}" \
        --argjson memory_total_mb "${mem_total_mb}" \
        --argjson load_1m "${load_1m}" \
        --argjson load_5m "${load_5m}" \
        --argjson load_15m "${load_15m}" \
        --arg version "${VERSION}" \
        --arg status_message "OK" \
        --arg wireguard_ip "${WIREGUARD_IP}" \
        '{
            hostname: $hostname,
            uptime_seconds: $uptime_seconds,
            disk_available_gb: $disk_available_gb,
            memory_used_mb: $memory_used_mb,
            memory_total_mb: $memory_total_mb,
            load_1m: $load_1m,
            load_5m: $load_5m,
            load_15m: $load_15m,
            version: $version,
            status_message: $status_message,
            wireguard_ip: $wireguard_ip
        }'
)"

echo "[INFO] Sending status to ${API_URL}"

curl -fsSL \
    -u "${API_USER}:${API_PASS}" \
    -H 'Content-Type: application/json' \
    -d "${json_payload}" \
    "${API_URL}"

echo "[SUCCESS] Status posted"
