#!/usr/bin/env python3
"""Extract Private Internet Access connection-entry IPs from official v6 catalog.

Includes WireGuard, OpenVPN TCP, and OpenVPN UDP endpoint IPs. Deliberately
excludes `meta` service addresses because those are control/service endpoints,
not VPN tunnel entry endpoints.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROVIDER = "pia"
PROTOCOL_KEYS = ("wg", "ovpntcp", "ovpnudp")


def safe(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def read_catalog(path: Path) -> object:
    # PIA's endpoint may append non-JSON signature material after the first line.
    text = path.read_text(encoding="utf-8", errors="strict")
    first = text.splitlines()[0] if "\n" in text else text
    return json.loads(first)


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract PIA WireGuard/OpenVPN entry endpoint IPs from official serverlist v6")
    parser.add_argument("source")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source = Path(args.source)
    payload = read_catalog(source)
    if not isinstance(payload, dict) or not isinstance(payload.get("regions"), list):
        raise SystemExit("expected PIA catalog object with regions array")

    rows: list[tuple[str, str, str]] = []
    region_count = 0
    protocol_records = {key: 0 for key in PROTOCOL_KEYS}
    missing_ip = 0

    for rindex, region in enumerate(payload["regions"]):
        if not isinstance(region, dict):
            continue
        region_count += 1
        servers = region.get("servers")
        if not isinstance(servers, dict):
            continue
        region_id = region.get("id", "")
        region_name = region.get("name", "")
        for protocol in PROTOCOL_KEYS:
            records = servers.get(protocol)
            if not isinstance(records, list):
                continue
            for sindex, server in enumerate(records):
                if not isinstance(server, dict):
                    continue
                protocol_records[protocol] += 1
                address = server.get("ip")
                if not isinstance(address, str) or not address.strip():
                    missing_ip += 1
                    continue
                provenance = safe(
                    f"official-serverlist-v6|region={region_id}|region-name={region_name}|protocol={protocol}|index={sindex}|cn={server.get('cn','')}"
                )
                rows.append((PROVIDER, address.strip(), provenance))

    if not rows:
        raise SystemExit("no PIA VPN connection endpoint IPs extracted")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write("\t".join(row) + "\n")

    print(f"provider={PROVIDER}")
    print(f"regions={region_count}")
    for protocol in PROTOCOL_KEYS:
        print(f"{protocol}-records={protocol_records[protocol]}")
    print("meta-records=EXCLUDED")
    print(f"missing-ip={missing_ip}")
    print(f"adapter-output-rows={len(rows)}")
    print(f"output={output}")
    print("PIA ADAPTER: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
