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

    if not isinstance(data, list):
        raise RuntimeError(
            "Expected Nord top-level list"
        )

    if len(data) < 1000:
        raise RuntimeError(
            "Nord server catalog is "
            "suspiciously small"
        )

    rows = []

    online_records = 0
    offline_records = 0
    missing_station = 0
    station_crosscheck_failures = 0
    extra_entry_records = 0

    for server_index, server in enumerate(data):
        if not isinstance(server, dict):
            raise RuntimeError(
                f"Server record {server_index} "
                f"is not an object"
            )

        status = server.get("status")

        if status != "online":
            offline_records += 1
            continue

        online_records += 1

        station = server.get("station")

        if not isinstance(
            station,
            str,
        ) or not station.strip():
            missing_station += 1
            continue

        station = station.strip()

        hostname = clean(
            server.get("hostname")
        )

        server_id = clean(
            server.get("id")
        )

        #
        # Provider-specific schema cross-check only.
        #
        # Nord also exposes ips[] records with
        # type="entry". We require the station value
        # to appear among those entry records.
        #
        # We deliberately do NOT validate whether the
        # value is an IP address here. That belongs to
        # the normalization engine.
        #

        entry_values = []

        ips = server.get("ips")

        if not isinstance(ips, list):
            station_crosscheck_failures += 1
            continue

        for item in ips:
            if not isinstance(item, dict):
                continue

            if item.get("type") != "entry":
                continue

            ip_object = item.get("ip")

            if not isinstance(ip_object, dict):
                continue

            raw_entry = ip_object.get("ip")

            if isinstance(
                raw_entry,
                str,
            ) and raw_entry.strip():
                entry_values.append(
                    raw_entry.strip()
                )

        if station not in entry_values:
            station_crosscheck_failures += 1
            continue

        #
        # An entry record other than station is not
        # automatically an error. We already observed
        # Nord's documentation IPv6 placeholder
        # 2001:db8:1::1 on one server.
        #

        extra_entry_records += sum(
            1
            for value in entry_values
            if value != station
        )

        provenance = (
            f"ServerID={server_id};"
            f"Hostname={hostname};"
            f"Status=online;"
            f"field=station"
        )

        #
        # IMPORTANT:
        #
        # No IP parsing.
        # No canonicalization.
        # No deduplication.
        # No sorting.
        # No CIDR conversion.
        # No collapsing.
        #
        # Those operations belong exclusively to the
        # normalization engine.
        #

        rows.append(
            (
                "nord",
                station,
                provenance,
            )
        )

    if online_records < 1000:
        raise RuntimeError(
            "Nord online catalog is "
            "suspiciously small"
        )

    if missing_station:
        raise RuntimeError(
            f"{missing_station} online Nord "
            f"servers had no station value"
        )

    if station_crosscheck_failures:
        raise RuntimeError(
            f"{station_crosscheck_failures} Nord "
            f"station values failed ips[] "
            f"entry cross-check"
        )

    if len(rows) < 1000:
        raise RuntimeError(
            "Nord adapter produced a "
            "suspiciously small dataset"
        )

    output.write_text(
        "".join(
            f"{provider}\t{ip}\t{provenance}\n"
            for provider, ip, provenance in rows
        ),
        encoding="utf-8",
    )

    unique_raw = {
        ip
        for _, ip, _ in rows
    }

    print(
        f"catalog-server-records={len(data)}"
    )

    print(
        f"online-server-records={online_records}"
    )

    print(
        f"offline-server-records={offline_records}"
    )

    print(
        f"adapter-output-rows={len(rows)}"
    )

    print(
        f"adapter-unique-raw-ip-strings="
        f"{len(unique_raw)}"
    )

    print(
        f"missing-station={missing_station}"
    )

    print(
        "station-crosscheck-failures="
        f"{station_crosscheck_failures}"
    )

    print(
        f"extra-entry-records="
        f"{extra_entry_records}"
    )

    print(
        f"output={output}"
    )

    print(
        "NORD ADAPTER: PASS"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"NORD ADAPTER: FAIL: {error}",
            file=sys.stderr,
        )
        sys.exit(1)
