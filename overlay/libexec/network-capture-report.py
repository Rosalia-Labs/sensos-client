#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import struct
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PCAP_MAGIC_USEC_LE = 0xD4C3B2A1
PCAP_MAGIC_USEC_BE = 0xA1B2C3D4
PCAP_MAGIC_NSEC_LE = 0x4D3CB2A1
PCAP_MAGIC_NSEC_BE = 0xA1B23C4D

LINKTYPE_ETHERNET = 1
LINKTYPE_LINUX_SLL = 113

ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_IPV6 = 0x86DD
ETHERTYPE_VLAN = 0x8100
ETHERTYPE_QINQ = 0x88A8

PROTO_TCP = 6
PROTO_UDP = 17
PROTO_ICMP = 1
PROTO_ICMPV6 = 58


@dataclass
class PacketSummary:
    timestamp: float
    direction: str
    protocol: str
    bytes_on_wire: int
    local_ip: str | None
    remote_ip: str | None
    local_port: int | None
    remote_port: int | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize bounded SensOS packet captures."
    )
    parser.add_argument(
        "--capture-root",
        default=os.environ.get("SENSOS_NETWORK_CAPTURE_ROOT", "/sensos/log/network_capture/session"),
        help="Capture root directory containing the pcap ring buffer.",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="Only include capture files modified within the last N hours (default: 24). Use 0 to include all retained files.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="Number of rows to show in each top-N table.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON instead of human-readable tables.",
    )
    return parser.parse_args()


def shutil_which(cmd: str) -> str | None:
    return subprocess.run(
        ["sh", "-c", f"command -v {cmd}"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip() or None


def collect_local_ips() -> set[str]:
    local_ips: set[str] = {"127.0.0.1", "::1"}
    if not shutil_which("ip"):
        return local_ips

    result = subprocess.run(
        ["ip", "-o", "addr", "show"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return local_ips

    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        family = fields[2]
        address = fields[3]
        if family not in {"inet", "inet6"}:
            continue
        ip_text = address.split("/", 1)[0]
        try:
            local_ips.add(str(ipaddress.ip_address(ip_text)))
        except ValueError:
            continue
    return local_ips


def iter_capture_files(capture_root: Path, hours: float) -> list[Path]:
    pcap_dir = capture_root / "pcap"
    if not pcap_dir.is_dir():
        raise SystemExit(f"Error: pcap directory not found: {pcap_dir}")

    now = dt.datetime.now(dt.timezone.utc).timestamp()
    files = []
    for path in sorted(pcap_dir.glob("capture.pcap*")):
        if not path.is_file():
            continue
        if hours > 0:
            age_hours = (now - path.stat().st_mtime) / 3600.0
            if age_hours > hours:
                continue
        files.append(path)
    return files


def classify_pending_capture_error(exc: Exception) -> str | None:
    message = str(exc)
    if message in {
        "pcap file is truncated",
        "unsupported pcap magic number",
        "truncated packet header",
        "truncated packet payload",
    }:
        return message
    return None


def parse_pcap_header(handle) -> tuple[str, bool, int]:
    header = handle.read(24)
    if len(header) < 24:
        raise ValueError("pcap file is truncated")

    magic_le = struct.unpack("<I", header[:4])[0]
    magic_be = struct.unpack(">I", header[:4])[0]

    if magic_le == PCAP_MAGIC_USEC_LE:
        endian = "<"
        nanosecond = False
    elif magic_be == PCAP_MAGIC_USEC_BE:
        endian = ">"
        nanosecond = False
    elif magic_le == PCAP_MAGIC_NSEC_LE:
        endian = "<"
        nanosecond = True
    elif magic_be == PCAP_MAGIC_NSEC_BE:
        endian = ">"
        nanosecond = True
    else:
        raise ValueError("unsupported pcap magic number")

    _, _, _, _, _, linktype = struct.unpack(f"{endian}HHIIII", header[4:24])
    return endian, nanosecond, linktype


def parse_link_layer(linktype: int, payload: bytes) -> tuple[int | None, bytes]:
    if linktype == LINKTYPE_ETHERNET:
        if len(payload) < 14:
            return None, b""
        offset = 14
        ethertype = struct.unpack("!H", payload[12:14])[0]
        while ethertype in {ETHERTYPE_VLAN, ETHERTYPE_QINQ} and len(payload) >= offset + 4:
            ethertype = struct.unpack("!H", payload[offset + 2:offset + 4])[0]
            offset += 4
        return ethertype, payload[offset:]

    if linktype == LINKTYPE_LINUX_SLL:
        if len(payload) < 16:
            return None, b""
        ethertype = struct.unpack("!H", payload[14:16])[0]
        return ethertype, payload[16:]

    return None, b""


def parse_transport_ports(protocol_number: int, payload: bytes) -> tuple[int | None, int | None]:
    if protocol_number not in {PROTO_TCP, PROTO_UDP}:
        return None, None
    if len(payload) < 4:
        return None, None
    src_port, dst_port = struct.unpack("!HH", payload[:4])
    return src_port, dst_port


def protocol_name(protocol_number: int) -> str:
    if protocol_number == PROTO_TCP:
        return "tcp"
    if protocol_number == PROTO_UDP:
        return "udp"
    if protocol_number == PROTO_ICMP:
        return "icmp"
    if protocol_number == PROTO_ICMPV6:
        return "icmp6"
    return f"ipproto-{protocol_number}"


def parse_ip_packet(
    ethertype: int | None, payload: bytes, packet_length: int
) -> tuple[str, str, str, int | None, int | None, int] | None:
    if ethertype == ETHERTYPE_IPV4:
        if len(payload) < 20:
            return None
        version_ihl = payload[0]
        if version_ihl >> 4 != 4:
            return None
        ihl = (version_ihl & 0x0F) * 4
        if ihl < 20 or len(payload) < ihl:
            return None
        total_length = struct.unpack("!H", payload[2:4])[0]
        protocol_number = payload[9]
        src = str(ipaddress.IPv4Address(payload[12:16]))
        dst = str(ipaddress.IPv4Address(payload[16:20]))
        src_port, dst_port = parse_transport_ports(protocol_number, payload[ihl:])
        ip_bytes = total_length if total_length > 0 else packet_length
        return src, dst, protocol_name(protocol_number), src_port, dst_port, ip_bytes

    if ethertype == ETHERTYPE_IPV6:
        if len(payload) < 40:
            return None
        if payload[0] >> 4 != 6:
            return None
        payload_length = struct.unpack("!H", payload[4:6])[0]
        next_header = payload[6]
        src = str(ipaddress.IPv6Address(payload[8:24]))
        dst = str(ipaddress.IPv6Address(payload[24:40]))
        src_port, dst_port = parse_transport_ports(next_header, payload[40:])
        ip_bytes = payload_length + 40
        return src, dst, protocol_name(next_header), src_port, dst_port, ip_bytes

    return None


def classify_direction(
    src: str,
    dst: str,
    src_port: int | None,
    dst_port: int | None,
    local_ips: set[str],
) -> tuple[str, str | None, str | None, int | None, int | None]:
    src_is_local = src in local_ips
    dst_is_local = dst in local_ips

    if src_is_local and not dst_is_local:
        return "outbound", src, dst, src_port, dst_port
    if dst_is_local and not src_is_local:
        return "inbound", dst, src, dst_port, src_port
    if src_is_local and dst_is_local:
        return "local", src, dst, src_port, dst_port
    return "external", None, None, None, None


def iter_packet_summaries(path: Path, local_ips: set[str]) -> Iterable[PacketSummary]:
    with path.open("rb") as handle:
        endian, nanosecond, linktype = parse_pcap_header(handle)
        packet_header_struct = struct.Struct(f"{endian}IIII")

        while True:
            header = handle.read(packet_header_struct.size)
            if not header:
                return
            if len(header) < packet_header_struct.size:
                raise ValueError("truncated packet header")

            ts_sec, ts_frac, incl_len, orig_len = packet_header_struct.unpack(header)
            payload = handle.read(incl_len)
            if len(payload) < incl_len:
                raise ValueError("truncated packet payload")

            ethertype, network_payload = parse_link_layer(linktype, payload)
            parsed = parse_ip_packet(ethertype, network_payload, orig_len)
            if parsed is None:
                continue

            src, dst, protocol, src_port, dst_port, ip_bytes = parsed
            direction, local_ip, remote_ip, local_port, remote_port = classify_direction(
                src, dst, src_port, dst_port, local_ips
            )
            timestamp = ts_sec + (ts_frac / (1_000_000_000 if nanosecond else 1_000_000))
            yield PacketSummary(
                timestamp=timestamp,
                direction=direction,
                protocol=protocol,
                bytes_on_wire=ip_bytes,
                local_ip=local_ip,
                remote_ip=remote_ip,
                local_port=local_port,
                remote_port=remote_port,
            )


def bump(counter_map, key, byte_count: int) -> None:
    row = counter_map[key]
    row["packets"] += 1
    row["bytes"] += byte_count


def summarize(files: list[Path], local_ips: set[str]) -> dict:
    by_direction = defaultdict(lambda: {"packets": 0, "bytes": 0})
    by_protocol = defaultdict(lambda: {"packets": 0, "bytes": 0})
    by_remote = defaultdict(lambda: {"packets": 0, "bytes": 0})
    by_local_port = defaultdict(lambda: {"packets": 0, "bytes": 0})
    by_remote_port = defaultdict(lambda: {"packets": 0, "bytes": 0})
    by_flow = defaultdict(lambda: {"packets": 0, "bytes": 0})

    packet_count = 0
    skipped_files: dict[str, str] = {}
    pending_files: dict[str, str] = {}

    for path in files:
        try:
            if path.stat().st_size == 0:
                pending_files[str(path)] = "capture file exists but is still empty"
                continue
        except OSError as exc:
            skipped_files[str(path)] = str(exc)
            continue

        try:
            for packet in iter_packet_summaries(path, local_ips):
                packet_count += 1
                bump(by_direction, (packet.direction,), packet.bytes_on_wire)
                bump(by_protocol, (packet.direction, packet.protocol), packet.bytes_on_wire)
                if packet.remote_ip is not None:
                    bump(by_remote, (packet.direction, packet.remote_ip), packet.bytes_on_wire)
                if packet.local_port is not None:
                    bump(
                        by_local_port,
                        (packet.direction, packet.protocol, packet.local_port),
                        packet.bytes_on_wire,
                    )
                if packet.remote_port is not None:
                    bump(
                        by_remote_port,
                        (packet.direction, packet.protocol, packet.remote_port),
                        packet.bytes_on_wire,
                    )
                bump(
                    by_flow,
                    (
                        packet.direction,
                        packet.protocol,
                        packet.local_ip,
                        packet.local_port,
                        packet.remote_ip,
                        packet.remote_port,
                    ),
                    packet.bytes_on_wire,
                )
        except Exception as exc:
            pending_reason = classify_pending_capture_error(exc)
            if pending_reason is not None:
                pending_files[str(path)] = pending_reason
            else:
                skipped_files[str(path)] = str(exc)

    return {
        "meta": {
            "files_analyzed": len(files) - len(skipped_files),
            "files_skipped": len(skipped_files),
            "files_pending": len(pending_files),
            "packets_analyzed": packet_count,
            "local_ip_count": len(local_ips),
            "local_ips": sorted(local_ips),
            "skipped_files": skipped_files,
            "pending_files": pending_files,
        },
        "by_direction": flatten_table(by_direction, ["direction"]),
        "by_protocol": flatten_table(by_protocol, ["direction", "protocol"]),
        "by_remote": flatten_table(by_remote, ["direction", "remote_ip"]),
        "by_local_port": flatten_table(by_local_port, ["direction", "protocol", "local_port"]),
        "by_remote_port": flatten_table(by_remote_port, ["direction", "protocol", "remote_port"]),
        "by_flow": flatten_table(
            by_flow,
            ["direction", "protocol", "local_ip", "local_port", "remote_ip", "remote_port"],
        ),
    }


def flatten_table(counter_map, key_names: list[str]) -> list[dict]:
    rows = []
    for key, totals in counter_map.items():
        row = {name: value for name, value in zip(key_names, key)}
        row.update(totals)
        rows.append(row)
    rows.sort(key=lambda row: (-row["bytes"], -row["packets"]))
    return rows


def render_size(byte_count: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(byte_count)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{byte_count}B"


def print_table(title: str, rows: list[dict], columns: list[str], top_n: int) -> None:
    print(title)
    if not rows:
        print("  (no data)")
        return

    shown = rows[:top_n]
    widths = {}
    for column in columns:
        widths[column] = max(len(column), max(len(str(row.get(column, ""))) for row in shown))
    widths["packets"] = max(len("packets"), max(len(str(row["packets"])) for row in shown))
    widths["bytes"] = max(len("bytes"), max(len(render_size(row["bytes"])) for row in shown))

    header = "  " + "  ".join(
        f"{column:{widths[column]}}" for column in columns
    ) + f"  {'packets':>{widths['packets']}}  {'bytes':>{widths['bytes']}}"
    print(header)
    for row in shown:
        prefix = "  " + "  ".join(
            f"{str(row.get(column, '')):{widths[column]}}" for column in columns
        )
        print(
            f"{prefix}  {row['packets']:>{widths['packets']}}  {render_size(row['bytes']):>{widths['bytes']}}"
        )


def emit_text(summary: dict, args: argparse.Namespace) -> None:
    meta = summary["meta"]
    print("Network Capture Report")
    print(
        f"Files analyzed: {meta['files_analyzed']}  "
        f"Files pending: {meta['files_pending']}  "
        f"Files skipped: {meta['files_skipped']}  "
        f"Packets analyzed: {meta['packets_analyzed']}"
    )
    print(f"Hours requested: {args.hours}  Top rows per table: {args.top}")
    if meta["packets_analyzed"] == 0 and meta["files_pending"] > 0 and meta["files_skipped"] == 0:
        print("No readable capture data is available yet. This is normal if the session just started or the current pcap file has not been fully written yet.")
    print()
    print_table("Traffic by direction", summary["by_direction"], ["direction"], args.top)
    print()
    print_table("Traffic by direction and protocol", summary["by_protocol"], ["direction", "protocol"], args.top)
    print()
    print_table("Top remote peers", summary["by_remote"], ["direction", "remote_ip"], args.top)
    print()
    print_table("Top local ports", summary["by_local_port"], ["direction", "protocol", "local_port"], args.top)
    print()
    print_table("Top remote ports", summary["by_remote_port"], ["direction", "protocol", "remote_port"], args.top)
    print()
    print_table(
        "Top flows",
        summary["by_flow"],
        ["direction", "protocol", "local_ip", "local_port", "remote_ip", "remote_port"],
        args.top,
    )
    if meta["files_skipped"]:
        print()
        print("Skipped files")
        for path, reason in meta["skipped_files"].items():
            print(f"  {path}: {reason}")
    if meta["pending_files"]:
        print()
        print("Pending files")
        for path, reason in meta["pending_files"].items():
            print(f"  {path}: {reason}")


def main() -> None:
    args = parse_args()

    capture_root = Path(args.capture_root)
    files = iter_capture_files(capture_root, args.hours)
    local_ips = collect_local_ips()
    summary = summarize(files, local_ips)

    if args.json:
        json.dump(summary, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return

    emit_text(summary, args)


if __name__ == "__main__":
    main()
