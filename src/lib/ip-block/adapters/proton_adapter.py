#!/usr/bin/env python3

from pathlib import Path
import argparse
import json
import sys


def clean(value):
    if value is None:
        return "-"

    return (
        str(value)
        .replace("\t", " ")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--source",
        required=True,
    )

    parser.add_argument(
        "--output",
        required=True,
    )

    args = parser.parse_args()

    source = Path(args.source)
    output = Path(args.output)

    if not source.is_file():
        raise RuntimeError(
            f"Source missing: {source}"
        )

    data = json.loads(
        source.read_text(
            encoding="utf-8",
            errors="strict",
        )
    )

    if not isinstance(data, dict):
        raise RuntimeError(
            "Expected Proton top-level object"
        )

    logicals = data.get(
        "LogicalServers"
    )

    if not isinstance(
        logicals,
        list,
    ):
        raise RuntimeError(
            "LogicalServers is missing "
            "or not a list"
        )

    if len(logicals) < 100:
        raise RuntimeError(
            "Proton LogicalServers catalog "
            "is suspiciously small"
        )

    rows = []

    physical_records = 0
    missing_entry_ip = 0

    for logical_index, logical in enumerate(
        logicals
    ):
        if not isinstance(
            logical,
            dict,
        ):
            raise RuntimeError(
                f"LogicalServers[{logical_index}] "
                f"is not an object"
            )

        logical_name = clean(
            logical.get("Name")
        )

        servers = logical.get(
            "Servers"
        )

        if not isinstance(
            servers,
            list,
        ):
            raise RuntimeError(
                f"LogicalServers[{logical_index}]."
                f"Servers is not a list"
            )

        for server_index, server in enumerate(
            servers
        ):
            physical_records += 1

            if not isinstance(
                server,
                dict,
            ):
                raise RuntimeError(
                    f"Server record is not an object: "
                    f"logical={logical_index} "
                    f"server={server_index}"
                )

            entry_ip = server.get(
                "EntryIP"
            )

            if not isinstance(
                entry_ip,
                str,
            ) or not entry_ip.strip():
                missing_entry_ip += 1
                continue

            domain = clean(
                server.get("Domain")
            )

            provenance = (
                f"LogicalServer={logical_name};"
                f"Domain={domain};"
                f"field=EntryIP"
            )

            #
            # IMPORTANT:
            # Do not normalize, validate, sort,
            # deduplicate, collapse, or infer here.
            #
            # The adapter's job ends after extraction.
            #

            rows.append(
                (
                    "proton",
                    entry_ip.strip(),
                    provenance,
                )
            )

    if physical_records < 500:
        raise RuntimeError(
            "Proton physical-server catalog "
            "is suspiciously small"
        )

    if missing_entry_ip:
        raise RuntimeError(
            f"{missing_entry_ip} Proton physical "
            f"server records had no EntryIP"
        )

    if len(rows) < 500:
        raise RuntimeError(
            "Proton adapter produced a "
            "suspiciously small dataset"
        )

    output.write_text(
        "".join(
            f"{provider}\t{ip}\t{provenance}\n"
            for provider, ip, provenance in rows
        ),
        encoding="utf-8",
    )

    unique_raw_ips = {
        ip
        for _, ip, _ in rows
    }

    print(
        f"logical-servers={len(logicals)}"
    )

    print(
        f"physical-server-records="
        f"{physical_records}"
    )

    print(
        f"adapter-output-rows={len(rows)}"
    )

    print(
        f"adapter-unique-raw-ip-strings="
        f"{len(unique_raw_ips)}"
    )

    print(
        f"missing-entry-ip={missing_entry_ip}"
    )

    print(
        f"output={output}"
    )

    print(
        "PROTON ADAPTER: PASS"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"PROTON ADAPTER: FAIL: {error}",
            file=sys.stderr,
        )
        sys.exit(1)
