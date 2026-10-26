#!/usr/bin/env python3

from pathlib import Path
from collections import defaultdict
import argparse
import ipaddress
import re
import sys


PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


def numeric_ip_sort(value):
    addr = ipaddress.ip_address(value)
    return (addr.version, int(addr))


def read_adapter_rows(paths):
    rows = []

    for path in paths:
        path = Path(path)

        if not path.is_file():
            raise RuntimeError(
                f"Adapter input does not exist: {path}"
            )

        with path.open(
            "r",
            encoding="utf-8",
            errors="strict",
        ) as handle:

            for line_number, raw_line in enumerate(
                handle,
                start=1,
            ):
                line = raw_line.rstrip("\n")

                if not line:
                    continue

                parts = line.split("\t")

                if len(parts) != 3:
                    raise RuntimeError(
                        f"{path}:{line_number}: "
                        f"expected exactly 3 TSV columns"
                    )

                provider, raw_ip, provenance = parts

                if not PROVIDER_RE.fullmatch(provider):
                    raise RuntimeError(
                        f"{path}:{line_number}: "
                        f"invalid provider name "
                        f"{provider!r}"
                    )

                if not raw_ip:
                    raise RuntimeError(
                        f"{path}:{line_number}: "
                        f"empty IP field"
                    )

                if not provenance:
                    raise RuntimeError(
                        f"{path}:{line_number}: "
                        f"empty provenance field"
                    )

                #
                # Adapter output is intentionally allowed
                # to contain duplicate IPs. Normalization
                # happens here and nowhere else.
                #

                try:
                    addr = ipaddress.ip_address(raw_ip)
                except ValueError as error:
                    raise RuntimeError(
                        f"{path}:{line_number}: "
                        f"invalid IP {raw_ip!r}"
                    ) from error

                if not addr.is_global:
                    raise RuntimeError(
                        f"{path}:{line_number}: "
                        f"non-global IP rejected: {addr}"
                    )

                canonical = str(addr)

                rows.append(
                    (
                        provider,
                        canonical,
                        provenance,
                    )
                )

    if not rows:
        raise RuntimeError(
            "No adapter records supplied"
        )

    return rows


def collapse_exact(addresses, version):
    networks = [
        ipaddress.ip_network(
            f"{address}/{32 if version == 4 else 128}",
            strict=True,
        )
        for address in addresses
    ]

    collapsed = list(
        ipaddress.collapse_addresses(networks)
    )

    represented = sum(
        network.num_addresses
        for network in collapsed
    )

    if represented != len(addresses):
        raise RuntimeError(
            f"IPv{version} collapse represented "
            f"{represented} addresses but source had "
            f"{len(addresses)} unique addresses"
        )

    #
    # Belt-and-suspenders verification:
    # every address emitted by an adapter must still be
    # represented by the collapsed output.
    #

    for address in addresses:
        addr = ipaddress.ip_address(address)

        if not any(
            addr in network
            for network in collapsed
        ):
            raise RuntimeError(
                f"Collapsed IPv{version} output lost "
                f"source address {address}"
            )

    return collapsed


def write_lines(path, values):
    Path(path).write_text(
        "".join(
            f"{value}\n"
            for value in values
        ),
        encoding="ascii",
    )


def render_set(lines, family, name, values):
    addr_type = (
        "ipv4_addr"
        if family == 4
        else "ipv6_addr"
    )

    lines.extend([
        f"    set {name} {{",
        f"        type {addr_type}",
        "        flags interval",
        "        auto-merge",
    ])

    if values:
        lines.append("        elements = {")

        for index, network in enumerate(values):
            comma = (
                ","
                if index < len(values) - 1
                else ""
            )

            lines.append(
                f"            "
                f"{network.with_prefixlen}{comma}"
            )

        lines.append("        }")

    lines.append("    }")


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--input",
        action="append",
        required=True,
        help=(
            "Adapter TSV input. May be specified "
            "multiple times."
        ),
    )

    parser.add_argument(
        "--out-dir",
        required=True,
    )

    args = parser.parse_args()

    output = Path(args.out_dir)

    output.mkdir(
        parents=True,
        exist_ok=True,
    )

    rows = read_adapter_rows(
        args.input
    )

    providers = sorted(
        {
            provider
            for provider, _, _ in rows
        }
    )

    #
    # Deduplicate addresses globally.
    #

    ipv4 = sorted(
        {
            address
            for _, address, _ in rows
            if ipaddress.ip_address(address).version == 4
        },
        key=numeric_ip_sort,
    )

    ipv6 = sorted(
        {
            address
            for _, address, _ in rows
            if ipaddress.ip_address(address).version == 6
        },
        key=numeric_ip_sort,
    )

    if not ipv4 and not ipv6:
        raise RuntimeError(
            "Normalization produced zero addresses"
        )

    #
    # Keep provenance independently.
    #

    provenance = defaultdict(set)

    for provider, address, source in rows:
        provenance[address].add(
            (
                provider,
                source,
            )
        )

    v4_cidrs = collapse_exact(
        ipv4,
        4,
    )

    v6_cidrs = collapse_exact(
        ipv6,
        6,
    )

    write_lines(
        output / "normalized-ipv4.txt",
        ipv4,
    )

    write_lines(
        output / "normalized-ipv6.txt",
        ipv6,
    )

    write_lines(
        output / "normalized-ipv4-cidrs.txt",
        [
            network.with_prefixlen
            for network in v4_cidrs
        ],
    )

    write_lines(
        output / "normalized-ipv6-cidrs.txt",
        [
            network.with_prefixlen
            for network in v6_cidrs
        ],
    )

    with (
        output / "provenance.tsv"
    ).open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as handle:

        for address in sorted(
            provenance,
            key=numeric_ip_sort,
        ):
            for provider, source in sorted(
                provenance[address]
            ):
                handle.write(
                    f"{address}\t"
                    f"{provider}\t"
                    f"{source}\n"
                )

    #
    # Produce an unhooked nftables representation.
    #
    # This contains sets only. It cannot affect traffic.
    #

    nft_lines = [
        "table inet vpn_normalizer_lab {",
    ]

    render_set(
        nft_lines,
        4,
        "normalized_ipv4",
        v4_cidrs,
    )

    render_set(
        nft_lines,
        6,
        "normalized_ipv6",
        v6_cidrs,
    )

    nft_lines.extend([
        "}",
        "",
    ])

    (
        output / "normalized.nft"
    ).write_text(
        "\n".join(nft_lines),
        encoding="ascii",
    )

    print(
        f"adapter-input-rows={len(rows)}"
    )

    print(
        f"providers={len(providers)}"
    )

    print(
        "provider-list="
        + ",".join(providers)
    )

    print(
        f"unique-ipv4={len(ipv4)}"
    )

    print(
        f"unique-ipv6={len(ipv6)}"
    )

    print(
        f"ipv4-cidrs={len(v4_cidrs)}"
    )

    print(
        f"ipv6-cidrs={len(v6_cidrs)}"
    )

    print(
        "ipv4-represented-addresses="
        f"{sum(n.num_addresses for n in v4_cidrs)}"
    )

    print(
        "ipv6-represented-addresses="
        f"{sum(n.num_addresses for n in v6_cidrs)}"
    )

    print(
        "NORMALIZATION ENGINE: PASS"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"NORMALIZATION ENGINE: FAIL: {error}",
            file=sys.stderr,
        )
        sys.exit(1)
