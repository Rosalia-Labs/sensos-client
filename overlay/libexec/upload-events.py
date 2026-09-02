#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

"""Drain the local event spool to the server.

Runs from sensos-upload-events.service (timer-driven). Cheap when the spool is
empty. Events stay queued until the server accepts them, so an offline window
(reboot, network reconfiguration) never loses an event.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
from pathlib import Path
from urllib import error, request

SCRIPT_FILE = Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_FILE.parent
OVERLAY_ROOT = Path(os.environ.get("SENSOS_CLIENT_ROOT", "/sensos"))
UTILS_FILE = OVERLAY_ROOT / "libexec" / "utils.py"
sys.path.insert(0, str(SCRIPT_DIR))

from events_data import (  # noqa: E402
    connect_db,
    mark_sent,
    prune_sent,
    select_pending,
    utcnow_text,
)

if not UTILS_FILE.is_file():
    raise RuntimeError(f"Missing utils.py at {UTILS_FILE}")

UTILS_SPEC = importlib.util.spec_from_file_location("sensos_overlay_utils", UTILS_FILE)
UTILS_MODULE = importlib.util.module_from_spec(UTILS_SPEC)
assert UTILS_SPEC.loader is not None
UTILS_SPEC.loader.exec_module(UTILS_MODULE)

for _name in dir(UTILS_MODULE):
    if not _name.startswith("_"):
        globals()[_name] = getattr(UTILS_MODULE, _name)

BATCH_LIMIT = 500
MAX_BATCHES_PER_RUN = 20


def post_events(server_ip: str, port: str, peer_uuid: str, api_password: str, payload: dict, timeout: int = 15) -> None:
    api_url = f"http://{server_ip}:{port}/api/v1/client/peer/events"
    req = request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **build_basic_auth_header(api_password, username=peer_uuid),
        },
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as response:
        response.read()


def main() -> int:
    version = read_client_version_text(str(OVERLAY_ROOT))
    config = read_network_conf()
    if not config:
        raise SystemExit("[ERROR] network.conf missing; run config-network first.")
    peer_uuid = require_peer_uuid(config)
    api_password = read_service_credential("api_password")
    server_ip = config.get("SERVER_WG_IP")
    server_port = config.get("SERVER_PORT")
    if not server_ip or not server_port:
        raise SystemExit("[ERROR] SERVER_WG_IP or SERVER_PORT missing in network.conf.")

    hostname = socket.gethostname()
    conn = connect_db()
    total_sent = 0
    try:
        with conn:
            prune_sent(conn)

        for _ in range(MAX_BATCHES_PER_RUN):
            with conn:
                pending = select_pending(conn, BATCH_LIMIT)
            if not pending:
                break

            payload = {
                "hostname": hostname,
                "client_version": version,
                "sent_at": utcnow_text(),
                "events": [
                    {
                        "id": row["event_uuid"],
                        "occurred_at": row["occurred_at"],
                        "event_type": row["event_type"],
                        "severity": row["severity"],
                        "details": json.loads(row["details_json"] or "{}"),
                    }
                    for row in pending
                ],
            }

            print(
                f"[INFO] Uploading {len(pending)} event(s) to "
                f"http://{server_ip}:{server_port}/api/v1/client/peer/events"
            )
            try:
                post_events(server_ip, server_port, peer_uuid, api_password, payload)
            except error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                raise SystemExit(f"[ERROR] Event upload failed: HTTP {exc.code}: {body}") from exc
            except Exception as exc:
                raise SystemExit(f"[ERROR] Event upload failed: {exc}") from exc

            with conn:
                mark_sent(conn, [row["id"] for row in pending])
            total_sent += len(pending)
            if len(pending) < BATCH_LIMIT:
                break
    finally:
        conn.close()

    if total_sent:
        print(f"[SUCCESS] Uploaded {total_sent} event(s)")
    else:
        print("[INFO] No pending events.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
