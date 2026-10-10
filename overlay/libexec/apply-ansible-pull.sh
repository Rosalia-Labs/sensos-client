#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Pulls and applies the configured Ansible playbook. Run periodically
# (sensos-ansible-pull.timer) rather than only on repo changes: a playbook
# is expected to be idempotent, so re-applying it every tick corrects drift
# caused by anything else touching the system, the same reconciliation
# philosophy as apply-uplink-cap.sh.
#
# Config lives in ansible.conf (written by config-ansible). There is no
# built-in default repo here -- a playbook source is site-specific -- so
# this does nothing when ansible.conf is absent.
#
# Runs as root (this unit has no User=), the standard way to run
# ansible-pull so playbooks can manage system state directly. Auth to the
# git host uses the sensos-admin SSH key (the same keypair config-network
# registers with the sensos-server) rather than a root-owned key, so
# there's a single key to rotate/revoke, not two.

set -euo pipefail

resolve_overlay_root() {
    printf '%s\n' "${SENSOS_CLIENT_ROOT:-/sensos}"
}

OVERLAY_ROOT="$(resolve_overlay_root)"
ANSIBLE_CONF="${OVERLAY_ROOT}/etc/ansible.conf"
SENSOS_ADMIN_SSH_KEY="/home/sensos-admin/.ssh/id_ed25519"

if [[ ! -f "${ANSIBLE_CONF}" ]]; then
    echo "No ${ANSIBLE_CONF}; ansible-pull not configured, nothing to do."
    exit 0
fi
# shellcheck disable=SC1090
source "${ANSIBLE_CONF}"

if [[ -z "${ANSIBLE_REPO_URL:-}" ]]; then
    echo "No ANSIBLE_REPO_URL in ${ANSIBLE_CONF}; nothing to do."
    exit 0
fi

command -v ansible-pull >/dev/null 2>&1 || {
    echo "ERROR: missing required command: ansible-pull (expected package ansible-core)" >&2
    exit 1
}

# Only meaningful for an ssh:// or git@host:path URL -- git ignores this
# for https:// transports, so it's harmless to always set.
if [[ -f "${SENSOS_ADMIN_SSH_KEY}" ]]; then
    export GIT_SSH_COMMAND="ssh -i ${SENSOS_ADMIN_SSH_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
fi

args=(-U "${ANSIBLE_REPO_URL}")
[[ -n "${ANSIBLE_BRANCH:-}" ]] && args+=(-C "${ANSIBLE_BRANCH}")
[[ -n "${ANSIBLE_INVENTORY:-}" ]] && args+=(-i "${ANSIBLE_INVENTORY}")
args+=("${ANSIBLE_PLAYBOOK:-local.yml}")

echo "Running: ansible-pull ${args[*]}"
exec ansible-pull "${args[@]}"
