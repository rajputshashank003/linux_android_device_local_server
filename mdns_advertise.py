#!/usr/bin/env python3
"""Advertise myserver.local on the current IPv4 mDNS link using the stdlib."""
import fcntl
import os
from pathlib import Path
import socket
import struct
import tempfile
import time

from lan_network import current_wifi_ipv4

GROUP = "224.0.0.251"
PORT = 5353
NAME = "myserver.local"
TTL = 120
POLL = 2.0


def current_lan_ipv4():
    """Prefer the assigned wlan interface; do not depend on restricted netlink APIs."""
    return current_wifi_ipv4()


def encode_name(name):
    return b"".join(bytes((len(label),)) + label.encode("ascii")
                    for label in name.rstrip(".").split(".")) + b"\0"


def read_name(packet, offset):
    labels = []
    end = None
    seen = set()
    while offset < len(packet):
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("truncated DNS pointer")
            pointer = ((length & 0x3F) << 8) | packet[offset + 1]
            if pointer in seen:
                raise ValueError("DNS pointer loop")
            seen.add(pointer)
            if end is None:
                end = offset + 2
            offset = pointer
            continue
        offset += 1
        if length == 0:
            return ".".join(labels).lower(), (end if end is not None else offset)
        if length & 0xC0 or offset + length > len(packet):
            raise ValueError("invalid DNS label")
        labels.append(packet[offset:offset + length].decode("ascii"))
        offset += length
    raise ValueError("unterminated DNS name")


def a_answer(address):
    return (struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + encode_name(NAME)
            + struct.pack("!HHIH", 1, 0x8001, TTL, 4)
            + socket.inet_aton(address))


def nsec_no_aaaa():
    """RFC 6762 NSEC: this unique name has A and NSEC, but no AAAA."""
    bitmap = b"\x40\x00\x00\x00\x00\x01"  # window 0: A (1), NSEC (47)
    rdata = encode_name(NAME) + b"\x00\x06" + bitmap
    rr = encode_name(NAME) + struct.pack("!HHIH", 47, 0x8001, TTL, len(rdata)) + rdata
    return struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + rr


def multicast_answer(address, matching):
    wants_a = any(qtype in (1, 255) for qtype, _ in matching)
    wants_no_aaaa = any(qtype in (28, 255) for qtype, _ in matching)
    records = []
    if wants_a:
        records.append(a_answer(address)[12:])
    if wants_no_aaaa:
        records.append(nsec_no_aaaa()[12:])
    return struct.pack("!HHHHHH", 0, 0x8400, 0, len(records), 0, 0) + b"".join(records)


def announce(sock, address):
    """Send two startup announcements as required by RFC 6762."""
    sock.sendto(a_answer(address), (GROUP, PORT))
    time.sleep(1.0)
    sock.sendto(a_answer(address), (GROUP, PORT))




def probe_packet(address):
    """Build an RFC 6762 unique-name probe with the proposed A record in Authority."""
    question = encode_name(NAME) + struct.pack("!HH", 255, 1)  # ANY, IN
    proposed = (encode_name(NAME) + struct.pack("!HHIH", 1, 1, TTL, 4)
                + socket.inet_aton(address))
    return struct.pack("!HHHHHH", 0, 0, 1, 0, 1, 0) + question + proposed


def resource_records(packet):
    """Return decoded resource records from all DNS sections."""
    if len(packet) < 12:
        return []
    _ident, _flags, qd, an, ns, ar = struct.unpack_from("!HHHHHH", packet, 0)
    offset = 12
    try:
        for _ in range(qd):
            _name, offset = read_name(packet, offset)
            offset += 4
            if offset > len(packet):
                return []
        result = []
        for section, count in (("answer", an), ("authority", ns), ("additional", ar)):
            for _ in range(count):
                name, offset = read_name(packet, offset)
                if offset + 10 > len(packet):
                    return []
                rtype, rclass, _ttl, length = struct.unpack_from("!HHIH", packet, offset)
                offset += 10
                if offset + length > len(packet):
                    return []
                data = packet[offset:offset + length]
                offset += length
                result.append((section, name, rtype, rclass, data))
        return result
    except (ValueError, UnicodeError, struct.error):
        return []


def conflicting_address(packet, address):
    """Return a competing A address from an answer or an opposing probe, if any."""
    flags = struct.unpack_from("!H", packet, 2)[0]
    local = socket.inet_aton(address)
    for section, name, rtype, rclass, data in resource_records(packet):
        if name != NAME or rtype != 1 or (rclass & 0x7FFF) != 1 or len(data) != 4 or data == local:
            continue
        # Existing answer is a definite conflict. During simultaneous probes,
        # RFC 6762 tie-breaking compares the proposed RDATA lexicographically.
        if flags & 0x8000 or (section == "authority" and data > local):
            return socket.inet_ntoa(data)
    return None


def should_yield_name(packet, address):
    """An established different A record or winning simultaneous probe means yield."""
    flags = struct.unpack_from("!H", packet, 2)[0]
    if flags & 0x8000:
        return conflicting_address(packet, address) is not None
    local = socket.inet_aton(address)
    return any(section == "authority" and name == NAME and rtype == 1
               and (rclass & 0x7FFF) == 1 and len(data) == 4 and data > local
               for section, name, rtype, rclass, data in resource_records(packet))


class NameConflict(Exception):
    pass


def probe_name(sock, address):
    """Probe before claiming the unique hostname; refuse an existing competing owner."""
    packet = probe_packet(address)
    for _ in range(3):
        sock.sendto(packet, (GROUP, PORT))
        deadline = time.monotonic() + 0.25
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                response, _peer = sock.recvfrom(9000)
            except socket.timeout:
                break
            conflict = conflicting_address(response, address)
            if conflict:
                raise NameConflict(f"{NAME} is already claimed by {conflict}; refusing to advertise {address}")
    sock.settimeout(POLL)

def parse_questions(packet):
    if len(packet) < 12:
        return [], 12
    flags = struct.unpack_from("!H", packet, 2)[0]
    if flags & 0x8000:
        return [], 12
    count = struct.unpack_from("!H", packet, 4)[0]
    offset = 12
    result = []
    for _ in range(count):
        name, offset = read_name(packet, offset)
        if offset + 4 > len(packet):
            raise ValueError("truncated DNS question")
        qtype, qclass = struct.unpack_from("!HH", packet, offset)
        offset += 4
        result.append((name, qtype, qclass))
    return result, offset


def legacy_unicast_answer(query, question_end, address, matching):
    """Build RFC 6762 section 6.7 response for a query from a non-5353 port."""
    query_id, query_flags, question_count = struct.unpack_from("!HHH", query, 0)
    questions = query[12:question_end]
    # Legacy resolvers require their transaction ID and question section echoed.
    # Their responses must not use the mDNS cache-flush class bit or long TTLs.
    flags = 0x8400 | (query_flags & 0x0100)  # response, authoritative, preserve RD
    wants_a = any(qtype in (1, 255) for qtype, _ in matching)
    header = struct.pack("!HHHHHH", query_id, flags, question_count, int(wants_a), 0, 0)
    answer = ((encode_name(NAME) + struct.pack("!HHIH", 1, 1, min(TTL, 10), 4)
               + socket.inet_aton(address)) if wants_a else b"")
    # Echoing the AAAA question in an authoritative NOERROR response with no
    # answer provides the legacy unicast NODATA result (without mDNS NSEC).
    return header + questions + answer


def open_socket(address):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(("", PORT))
    membership = socket.inet_aton(GROUP) + socket.inet_aton(address)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sock.settimeout(POLL)
    return sock


def main():
    lock_path = Path(tempfile.gettempdir()) / f"termux-mdns-{os.getuid()}.lock"
    lock = lock_path.open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another myserver.local advertiser is already running.")

    address = None
    sock = None
    route_missing_since = None
    last_route_error = None
    last_socket_error = None
    print(f"Advertising {NAME} with a 120 second TTL; Ctrl+C stops this responder.", flush=True)
    try:
        while True:
            now = time.monotonic()
            try:
                routed = current_lan_ipv4()
                if route_missing_since is not None:
                    print(f"Wi-Fi route restored; mDNS is using {routed}.", flush=True)
                route_missing_since = None
                last_route_error = None
            except OSError as exc:
                if route_missing_since is None or str(exc) != last_route_error:
                    route_missing_since = now
                    last_route_error = str(exc)
                    print(f"Wi-Fi route temporarily unavailable: {exc}", flush=True)
                if sock:
                    print(f"Wi-Fi address is unavailable; withdrawing the old mDNS responder on {address}.", flush=True)
                    sock.close()
                    sock = None
                address = None
                time.sleep(POLL)
                continue

            if routed != address or sock is None:
                if sock:
                    sock.close()
                try:
                    sock = open_socket(routed)
                except OSError as exc:
                    sock = None
                    address = None
                    message = str(exc)
                    if message != last_socket_error:
                        print(f"Cannot join mDNS on {routed}: {exc}", flush=True)
                        last_socket_error = message
                    time.sleep(POLL)
                    continue
                try:
                    probe_name(sock, routed)
                except NameConflict as exc:
                    sock.close()
                    sock = None
                    address = None
                    print(f"Hostname conflict: {exc}", flush=True)
                    raise SystemExit(2)
                address = routed
                last_socket_error = None
                print(f"Joined mDNS on {address}; answering for {NAME}.", flush=True)
                announce(sock, address)

            try:
                packet, peer = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError as exc:
                print(f"mDNS receive failed: {exc}", flush=True)
                sock.close()
                sock = None
                continue
            conflict = conflicting_address(packet, address)
            if conflict:
                if should_yield_name(packet, address):
                    print(f"Hostname conflict from {peer[0]}: {NAME} is also claimed by {conflict}; yielding the name.", flush=True)
                    raise SystemExit(2)
                print(f"Competing probe from {peer[0]} ({conflict}); defending {NAME} at {address}.", flush=True)
                try:
                    sock.sendto(a_answer(address), (GROUP, PORT))
                except OSError as exc:
                    print(f"Could not defend mDNS name: {exc}", flush=True)
                continue
            try:
                questions, question_end = parse_questions(packet)
            except (ValueError, UnicodeError, struct.error):
                continue
            matching = [(qtype, qclass) for name, qtype, qclass in questions
                        if name == NAME and qtype in (1, 28, 255)
                        and (qclass & 0x7FFF) in (1, 255)]
            if matching:
                # A non-5353 source port denotes a legacy one-shot resolver. Its
                # unicast response needs the original ID/question, plain IN class,
                # and a short TTL; port-5353 mDNS queries use normal ID-zero replies.
                legacy = peer[1] != PORT
                unicast = legacy or any(qclass & 0x8000 for _, qclass in matching)
                destination = peer if unicast else (GROUP, PORT)
                response = (legacy_unicast_answer(packet, question_end, address, matching)
                            if legacy else multicast_answer(address, matching))
                try:
                    sock.sendto(response, destination)
                    kind = "legacy unicast" if legacy else "mDNS"
                    types = ",".join({1: "A", 28: "AAAA", 255: "ANY"}[t] for t, _ in matching)
                    print(f"Answered {kind} myserver.local {types} query from {peer[0]}:{peer[1]} -> {address}", flush=True)
                except OSError as exc:
                    print(f"Could not answer mDNS query: {exc}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        if sock:
            sock.close()
        lock.close()
        print("mDNS responder stopped.", flush=True)


if __name__ == "__main__":
    main()
