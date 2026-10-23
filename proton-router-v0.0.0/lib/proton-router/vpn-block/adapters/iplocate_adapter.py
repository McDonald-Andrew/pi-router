#!/usr/bin/env python3
"""Extract IPv4 addresses from IPLocate's free proxy endpoint list.

The source contains one verified proxy endpoint per line in IPv4:port form.
Ports are validated and retained in provenance, while the adapter emits bare
IPv4 addresses for the router's existing whole-address blocking pipeline.
"""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path
import sys


PROVIDER = "iplocate"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract whole-address blocks from IPLocate free proxy endpoints"
        )
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source = Path(args.source)
    output = Path(args.output)

    if not source.is_file():
        raise RuntimeError(f"Source missing: {source}")

    rows: list[tuple[str, str, str]] = []
    endpoints: set[tuple[str, int]] = set()
    source_lines = 0
    duplicate_endpoints = 0

    with source.open(
        "r",
        encoding="utf-8",
        errors="strict",
    ) as handle:
        for line_number, raw in enumerate(handle, 1):
            endpoint = raw.strip()

            if not endpoint:
                continue

            source_lines += 1
            host, separator, port_text = endpoint.rpartition(":")

            if not separator or not host or not port_text:
                raise RuntimeError(
                    f"line {line_number}: expected IPv4:port endpoint"
                )

            try:
                address = ipaddress.ip_address(host)
            except ValueError as exc:
                raise RuntimeError(
                    f"line {line_number}: invalid IP address {host!r}"
                ) from exc

            if address.version != 4:
                raise RuntimeError(
                    f"line {line_number}: expected IPv4 address"
                )

            if not address.is_global:
                raise RuntimeError(
                    f"line {line_number}: non-global address rejected: {address}"
                )

            if not port_text.isascii() or not port_text.isdigit():
                raise RuntimeError(
                    f"line {line_number}: invalid port {port_text!r}"
                )

            port = int(port_text, 10)

            if not 1 <= port <= 65535:
                raise RuntimeError(
                    f"line {line_number}: port outside 1-65535: {port}"
                )

            key = (str(address), port)

            if key in endpoints:
                duplicate_endpoints += 1
                continue

            endpoints.add(key)
            provenance = (
                "free-proxy-list:all-proxies.txt"
                f"|line={line_number}|port={port}"
            )
            rows.append((PROVIDER, str(address), provenance))

    if not rows:
        raise RuntimeError("No IPLocate free proxy endpoints extracted")

    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write("\t".join(row) + "\n")

    print(f"provider={PROVIDER}")
    print(f"source-lines={source_lines}")
    print(f"duplicate-endpoints={duplicate_endpoints}")
    print(f"adapter-output-rows={len(rows)}")
    print(f"output={output}")
    print("IPLOCATE ADAPTER: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(
            f"IPLOCATE ADAPTER: FAIL: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1)
