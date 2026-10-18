#!/usr/bin/env python3
"""Find the active Android Wi-Fi IPv4 address without netlink permissions."""
import ipaddress
import os
import re
import socket
import subprocess


def _private(address):
    try:
        parsed = ipaddress.ip_address(address)
        return parsed.version == 4 and parsed.is_private and not parsed.is_loopback and not parsed.is_link_local
    except ValueError:
        return False


def _ifconfig_addresses():
    try:
        result = subprocess.run(["ifconfig", "-a"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return []
    entries = []
    current = None
    for line in result.stdout.splitlines():
        header = re.match(r"^([A-Za-z0-9_.:-]+)(?::\s+flags=|\s+Link encap:)", line)
        if header:
            current = {"name": header.group(1).rstrip(":"),
                       "up": "UP" in line and "RUNNING" in line,
                       "address": None}
            entries.append(current)
        if current:
            match = re.search(r"\binet(?:\s+addr:|\s+)([0-9]+(?:\.[0-9]+){3})\b", line)
            if match:
                current["address"] = match.group(1)
            if "UP" in line and "RUNNING" in line:
                current["up"] = True
    return entries


def current_wifi_ipv4():
    """Prefer an assigned wlan/wifi interface; fall back to the kernel route source."""
    requested = os.environ.get("TERMUX_LAN_INTERFACE")
    entries = _ifconfig_addresses()
    wifi_entries = [entry for entry in entries
                    if entry["name"].lower().startswith(("wlan", "wifi", "ap", "swlan"))]
    if requested:
        candidates = [entry for entry in entries if entry["name"] == requested and entry["up"]]
    else:
        candidates = [entry for entry in wifi_entries if entry["up"]]
    for entry in candidates:
        if entry["address"] and _private(entry["address"]):
            return entry["address"]
    if requested and any(entry["name"] == requested for entry in entries):
        raise OSError(f"Requested Wi-Fi interface {requested} is not up with a private IPv4 address")
    if wifi_entries and not candidates:
        raise OSError("No active Wi-Fi interface is up; refusing to advertise a cellular or VPN address")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Documentation-only TEST-NET-1; UDP connect selects a route and sends no packet.
        sock.connect(("192.0.2.1", 9))
        address = sock.getsockname()[0]
    finally:
        sock.close()
    if not _private(address):
        raise OSError(f"No private Wi-Fi IPv4 address available (selected {address})")
    return address
