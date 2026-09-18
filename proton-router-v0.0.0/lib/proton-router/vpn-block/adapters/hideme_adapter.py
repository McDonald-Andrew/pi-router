#!/usr/bin/env python3
"""Extract hide.me connection-entry IPs from the official server catalog.

Source schema is the one consumed by hide.me's official OpenWrt client:
v1/network/paid/en -> server records with hostname and recursive children.
Only *.hideservers.net connection hostnames are accepted.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket

PROVIDER = "hideme"
SUFFIX = ".hideservers.net"


def safe(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def resolve_host(host: str) -> list[str]:
    answers: list[str] = []
    for result in socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM):
        answers.append(result[4][0])
    return answers


def top_servers(payload: object) -> list[object]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("servers"), list):
        return payload["servers"]
    raise ValueError("expected root array or object with servers array")


def walk_server(record: object, path: str):
    if not isinstance(record, dict):
        yield path, None, "not-object"
        return

    hostname = record.get("hostname")
    missing = [key for key in ("id", "flag", "hostname", "displayName") if not record.get(key)]
    if missing:
        yield path, record, "missing:" + ",".join(missing)
    else:
        yield path, record, "ok"

    children = record.get("children")
    if isinstance(children, list):
        for index, child in enumerate(children):
            yield from walk_server(child, f"{path}.children[{index}]")


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract hide.me endpoints from the official server catalog")
    parser.add_argument("source", help="JSON returned by https://api.hide.me/v1/network/paid/en")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source = Path(args.source)
    payload = json.loads(source.read_text(encoding="utf-8"))
    servers = top_servers(payload)

    rows: list[tuple[str, str, str]] = []
    records = 0
    invalid_records = 0
    invalid_domain = 0
    unresolved: list[str] = []

    for path, record, state in (item for i, server in enumerate(servers) for item in walk_server(server, f"servers[{i}]")):
        records += 1
        if state != "ok" or not isinstance(record, dict):
            invalid_records += 1
            continue
        hostname = str(record["hostname"]).strip().rstrip(".")
        if not hostname.lower().endswith(SUFFIX):
            invalid_domain += 1
            continue
        try:
            addresses = resolve_host(hostname)
        except socket.gaierror:
            addresses = []
        if not addresses:
            unresolved.append(hostname)
            continue
        provenance = safe(f"official-api:v1/network/paid/en|path={path}|hostname={hostname}|id={record.get('id')}|flag={record.get('flag')}")
        for address in addresses:
            rows.append((PROVIDER, address, provenance))

    if invalid_domain:
        raise SystemExit(f"catalog contained {invalid_domain} hostname(s) outside hideservers.net")
    if not rows:
        raise SystemExit("no hide.me connection endpoint IPs extracted")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write("\t".join(row) + "\n")

    print(f"provider={PROVIDER}")
    print(f"top-level-servers={len(servers)}")
    print(f"server-records={records}")
    print(f"invalid-records={invalid_records}")
    print(f"invalid-domain={invalid_domain}")
    print(f"unresolved-hostnames={len(unresolved)}")
    print(f"adapter-output-rows={len(rows)}")
    print(f"output={output}")
    print("HIDEME ADAPTER: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
