#!/usr/bin/env python3
"""Extract Windscribe connection-entry IPs from official mob-v2 serverlist JSON.

Intentionally ignores ping_ip. Direct node IP fields and documented connection
host fields are treated as entry material; normalization remains engine-owned.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket

PROVIDER = "windscribe"
IP_KEYS = ("ip", "ip2", "ip3", "ip4", "ip5")
HOST_KEYS = ("hostname", "wg_endpoint", "ovpn_x509", "dns_hostname")


def safe(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def literal_ip(value: str) -> str | None:
    candidate = value.strip()
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, candidate)
            return candidate
        except OSError:
            pass
    return None


def resolve_host(host: str) -> list[str]:
    return [item[4][0] for item in socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)]


def walk(value: object, path: str = "$root"):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, f"{path}[{index}]")


def emit_host(rows, unresolved, host: str, provenance: str):
    direct = literal_ip(host)
    if direct is not None:
        rows.append((PROVIDER, direct, provenance))
        return
    try:
        addresses = resolve_host(host)
    except socket.gaierror:
        addresses = []
    if not addresses:
        unresolved.append(host)
        return
    for address in addresses:
        rows.append((PROVIDER, address, provenance))


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract Windscribe entry endpoints from official serverlist/mob-v2 JSON")
    parser.add_argument("sources", nargs="+", help="One or more mob-v2 JSON responses (for example free/pro lists)")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    rows: list[tuple[str, str, str]] = []
    source_count = 0
    object_count = 0
    direct_fields = 0
    host_fields = 0
    ignored_ping_fields = 0
    unresolved: list[str] = []

    for source_arg in args.sources:
        source = Path(source_arg)
        payload = json.loads(source.read_text(encoding="utf-8"))
        source_count += 1
        for path, obj in walk(payload):
            object_count += 1
            if "ping_ip" in obj:
                ignored_ping_fields += 1

            # Node IPs: accept only objects that look like server nodes, not arbitrary "ip" metadata.
            node_like = "hostname" in obj or ".nodes[" in path
            if node_like:
                for key in IP_KEYS:
                    value = obj.get(key)
                    if not isinstance(value, str) or not value.strip():
                        continue
                    direct = literal_ip(value)
                    if direct is None:
                        continue
                    direct_fields += 1
                    rows.append((PROVIDER, direct, safe(f"official-mob-v2|source={source.name}|path={path}|field={key}")))

            for key in HOST_KEYS:
                value = obj.get(key)
                if not isinstance(value, str) or not value.strip():
                    continue
                host_fields += 1
                emit_host(rows, unresolved, value.strip().rstrip("."), safe(f"official-mob-v2|source={source.name}|path={path}|field={key}|host={value.strip()}"))

    if source_count == 0 or not rows:
        raise SystemExit("no Windscribe connection endpoint IPs extracted")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write("\t".join(row) + "\n")

    print(f"provider={PROVIDER}")
    print(f"source-files={source_count}")
    print(f"json-objects={object_count}")
    print(f"direct-ip-fields={direct_fields}")
    print(f"hostname-fields={host_fields}")
    print(f"ignored-ping-ip-fields={ignored_ping_fields}")
    print(f"unresolved-hostnames={len(unresolved)}")
    print(f"adapter-output-rows={len(rows)}")
    print(f"output={output}")
    print("WINDSCRIBE ADAPTER: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
