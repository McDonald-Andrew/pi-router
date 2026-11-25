#!/usr/bin/env python3
"""Validate HaGeZi's dnsmasq feed and render Pi Router's DNS policy."""

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


DOMAIN_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?", re.ASCII)
ENTRY_COUNT = re.compile(r"# Number of entries: ([0-9]+)")

# The router itself needs these names even when it starts without a VPN.
ROUTER_DEPENDENCIES = {
    "github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "proton.me",
    "protonvpn.ch",
    "protonvpn.com",
}


def checked_domain(value):
    labels = value.split(".")
    if (
        not 1 < len(labels)
        or len(value) > 253
        or labels[-1].isdigit()
        or not all(DOMAIN_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError(f"invalid DNS name: {value[:100]!r}")
    return value


def read_rules(path, *, previous=False):
    domains = set()
    declared = None
    count = 0
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                if match := ENTRY_COUNT.fullmatch(line):
                    if declared is not None:
                        raise ValueError(f"{path}: duplicate entry count")
                    declared = int(match[1])
                continue
            if previous and line.startswith("server=/") and line.endswith("/#"):
                # Regenerate exceptions from the router's current dependencies.
                checked_domain(line[len("server=/"):-2].lower())
                continue
            if not line.startswith("local=/") or not line.endswith("/"):
                raise ValueError(
                    f"{path}: unexpected dnsmasq directive at line {number}"
                )
            domains.add(checked_domain(line[len("local=/"):-1].lower()))
            count += 1

    if not previous and (not domains or count != declared):
        raise ValueError(
            f"{path}: incomplete feed: {count} rules, {declared} declared"
        )
    return domains


def covered(domain, rules):
    while "." in domain:
        if domain in rules:
            return True
        domain = domain.split(".", 1)[1]
    return False


def render(domains, dependencies):
    blocked = {domain for domain in domains if not covered(domain, dependencies)}

    # Keep a parent block in effect for other names while giving the required
    # service a more specific route to the default upstream. Exact competing
    # local= rules and rules below the dependency were filtered out above.
    exceptions = {
        hostname for hostname in dependencies if covered(hostname, blocked)
    }
    lines = ["# Generated Pi Router DNS policy from the HaGeZi bypass feed."]
    lines.extend(f"server=/{name}/#" for name in sorted(exceptions))
    lines.extend(f"local=/{name}/" for name in sorted(blocked))
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--previous", type=Path, help="last applied HaGeZi config")
    parser.add_argument("--preserve-url", action="append", default=[])
    args = parser.parse_args()

    try:
        domains = read_rules(args.source)
        if args.previous is not None:
            try:
                domains.update(read_rules(args.previous, previous=True))
            except FileNotFoundError:
                pass  # The first successful refresh has no previously applied rules.
        dependencies = ROUTER_DEPENDENCIES | {
            checked_domain(urlsplit(url).hostname or "")
            for url in args.preserve_url
        }
        sys.stdout.write(render(domains, dependencies))
    except (OSError, ValueError) as exc:
        print(f"HaGeZi: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
