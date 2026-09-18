#!/usr/bin/env python3
"""Extract Surfshark OpenVPN connection-entry endpoints.

Adapter contract: writes provider<TAB>ip<TAB>provenance rows and deliberately
leaves canonicalization, global/non-global filtering, dedupe, sorting, and CIDR
collapse to normalization engine v1.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import socket
import sys
import zipfile

PROVIDER = "surfshark"


def safe(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def literal_ip(value: str) -> str | None:
    candidate = value.strip()
    unwrapped = candidate[1:-1] if candidate.startswith("[") and candidate.endswith("]") else candidate
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, unwrapped)
            return unwrapped
        except OSError:
            pass
    return None


def resolve_host(host: str) -> list[str]:
    answers: list[str] = []
    for result in socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM):
        answers.append(result[4][0])
    return answers


def iter_configs(path: Path):
    if path.is_dir():
        for child in sorted(path.rglob("*")):
            if child.is_file() and child.suffix.lower() in {".ovpn", ".conf"}:
                yield str(child), child.read_text(encoding="utf-8", errors="replace")
        return

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in sorted(archive.namelist()):
                if name.lower().endswith((".ovpn", ".conf")):
                    yield f"{path.name}:{name}", archive.read(name).decode("utf-8", errors="replace")
        return

    if path.is_file():
        yield str(path), path.read_text(encoding="utf-8", errors="replace")
        return

    raise FileNotFoundError(path)


def parse_remote(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";")):
        return None
    try:
        parts = shlex.split(stripped, comments=False, posix=True)
    except ValueError:
        return None
    if len(parts) < 2 or parts[0].lower() != "remote":
        return None
    return parts[1], parts[2] if len(parts) >= 3 else ""


def main() -> int:
    parser = argparse.ArgumentParser(description='Extract Surfshark connection-entry endpoints from official OpenVPN configuration material.')
    parser.add_argument("sources", nargs="+", help="Official .ovpn/.conf file, directory, or ZIP bundle")
    parser.add_argument("--output", required=True, help="TSV output path")
    args = parser.parse_args()

    rows: list[tuple[str, str, str]] = []
    config_count = 0
    remote_count = 0
    unresolved: list[str] = []

    for source_arg in args.sources:
        source = Path(source_arg)
        for label, text in iter_configs(source):
            config_count += 1
            for lineno, line in enumerate(text.splitlines(), 1):
                parsed = parse_remote(line)
                if parsed is None:
                    continue
                host, port = parsed
                remote_count += 1
                provenance = safe(f"official-openvpn|source={label}|line={lineno}|remote={host}|port={port}")
                direct = literal_ip(host)
                if direct is not None:
                    rows.append((PROVIDER, direct, provenance))
                    continue
                try:
                    answers = resolve_host(host)
                except socket.gaierror:
                    answers = []
                if not answers:
                    unresolved.append(f"{label}:{lineno}:{host}")
                    continue
                for address in answers:
                    rows.append((PROVIDER, address, provenance))

    if config_count == 0:
        raise SystemExit("no OpenVPN configuration files found")
    if remote_count == 0:
        raise SystemExit("no OpenVPN remote directives found")
    if not rows:
        raise SystemExit("no connection endpoint IPs extracted")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for provider, address, provenance in rows:
            handle.write(f"{provider}\t{address}\t{provenance}\n")

    print(f"provider={PROVIDER}")
    print(f"config-files={config_count}")
    print(f"remote-directives={remote_count}")
    print(f"adapter-output-rows={len(rows)}")
    print(f"unresolved-hostnames={len(unresolved)}")
    for item in unresolved[:10]:
        print(f"unresolved={safe(item)}")
    if len(unresolved) > 10:
        print(f"unresolved-more={len(unresolved)-10}")
    print(f"output={output}")
    print(f"{PROVIDER.upper()} ADAPTER: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
