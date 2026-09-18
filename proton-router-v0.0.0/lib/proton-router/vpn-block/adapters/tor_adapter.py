#!/usr/bin/env python3

import argparse
import ipaddress
import json
import re
import sys

from pathlib import Path


PROVIDER = "tor"

FP_RE = re.compile(
    r"^[0-9A-Fa-f]{40}$"
)


def fail(message):
    print(
        f"tor_adapter: {message}",
        file=sys.stderr,
    )
    raise SystemExit(1)


def parse_or_address(value):
    #
    # Onionoo relay or_addresses contain an IP address
    # plus a TCP port or port list.
    #
    # Examples:
    #   1.2.3.4:9001
    #   [2001:db8::1]:9001
    #
    # The adapter validates the IP syntactically but does
    # not canonicalize it and does not decide whether the
    # address is globally routable. Those are normalization
    # engine responsibilities.
    #
    if not isinstance(value, str) or not value:
        raise ValueError(
            "not a non-empty string"
        )

    if value.startswith("["):
        close = value.find("]")

        if (
            close <= 1
            or close + 1 >= len(value)
            or value[close + 1] != ":"
        ):
            raise ValueError(
                "invalid bracketed address"
            )

        ip_text = value[1:close]
        port_text = value[close + 2:]

    else:
        if ":" not in value:
            raise ValueError(
                "missing port separator"
            )

        ip_text, port_text = value.rsplit(
            ":",
            1,
        )

    #
    # Validation only. Do not use the canonicalized return
    # value here.
    #
    ipaddress.ip_address(
        ip_text
    )

    if not port_text:
        raise ValueError(
            "missing port"
        )

    #
    # Onionoo documents this component as a TCP port or
    # port list. Accept decimal ports, comma-separated
    # lists, and ranges.
    #
    if not re.fullmatch(
        r"[0-9,\-]+",
        port_text,
    ):
        raise ValueError(
            "invalid port or port-list syntax"
        )

    return ip_text


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Extract public running Tor relay OR endpoint "
            "IPs from an Onionoo details response."
        )
    )

    parser.add_argument(
        "source",
        help="Onionoo details JSON",
    )

    parser.add_argument(
        "--output",
        required=True,
        help="Output provider TSV",
    )

    args = parser.parse_args()

    source = Path(
        args.source
    )

    output = Path(
        args.output
    )

    try:
        document = json.loads(
            source.read_text(
                encoding="utf-8"
            )
        )

    except Exception as exc:
        fail(
            f"unable to read JSON: {exc}"
        )

    if not isinstance(
        document,
        dict,
    ):
        fail(
            "top-level JSON is not an object"
        )

    relays = document.get(
        "relays"
    )

    if not isinstance(
        relays,
        list,
    ):
        fail(
            "relays is not an array"
        )

    truncated = document.get(
        "relays_truncated",
        0,
    )

    if truncated not in (
        0,
        None,
    ):
        fail(
            "response is truncated: "
            f"relays_truncated={truncated}"
        )

    published = document.get(
        "relays_published",
        "",
    )

    if (
        not isinstance(
            published,
            str,
        )
        or not published
    ):
        fail(
            "relays_published missing"
        )

    rows = []

    invalid_records = 0
    running_false = 0
    missing_fingerprint = 0
    missing_or_addresses = 0
    invalid_or_addresses = 0

    unique_ips = set()

    for relay in relays:
        if not isinstance(
            relay,
            dict,
        ):
            invalid_records += 1
            continue

        fingerprint = relay.get(
            "fingerprint"
        )

        if (
            not isinstance(
                fingerprint,
                str,
            )
            or not FP_RE.fullmatch(
                fingerprint
            )
        ):
            missing_fingerprint += 1
            continue

        #
        # Provider-specific consistency check:
        # this source was requested with running=true.
        #
        if relay.get(
            "running"
        ) is not True:
            running_false += 1
            continue

        #
        # ONLY public relay onion-routing connection
        # endpoints are accepted.
        #
        or_addresses = relay.get(
            "or_addresses"
        )

        if (
            not isinstance(
                or_addresses,
                list,
            )
            or not or_addresses
        ):
            missing_or_addresses += 1
            continue

        parsed = []

        for raw in or_addresses:
            try:
                ip_text = parse_or_address(
                    raw
                )

            except (
                ValueError,
                TypeError,
            ):
                invalid_or_addresses += 1
                continue

            parsed.append(
                (
                    ip_text,
                    raw,
                )
            )

        for ip_text, raw in parsed:
            provenance = (
                "onionoo:"
                f"{fingerprint.upper()}:"
                f"or_address={raw}"
            )

            rows.append(
                f"{PROVIDER}\t"
                f"{ip_text}\t"
                f"{provenance}\n"
            )

            unique_ips.add(
                ip_text
            )

    #
    # Be strict about provider-schema surprises.
    #
    if invalid_records:
        fail(
            "invalid relay objects="
            f"{invalid_records}"
        )

    if missing_fingerprint:
        fail(
            "invalid or missing fingerprints="
            f"{missing_fingerprint}"
        )

    if running_false:
        fail(
            "non-running relays in running=true response="
            f"{running_false}"
        )

    if missing_or_addresses:
        fail(
            "missing or empty or_addresses="
            f"{missing_or_addresses}"
        )

    if invalid_or_addresses:
        fail(
            "invalid or_addresses="
            f"{invalid_or_addresses}"
        )

    if not rows:
        fail(
            "no endpoint rows produced"
        )

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output.write_text(
        "".join(
            rows
        ),
        encoding="utf-8",
    )

    print(
        f"provider={PROVIDER}"
    )

    print(
        f"relays-published={published}"
    )

    print(
        f"relay-records={len(relays)}"
    )

    print(
        "adapter-output-rows="
        f"{len(rows)}"
    )

    print(
        "adapter-unique-raw-ip-strings="
        f"{len(unique_ips)}"
    )

    print(
        "invalid-relay-objects=0"
    )

    print(
        "invalid-or-addresses=0"
    )

    print(
        f"output={output}"
    )

    print(
        "TOR ADAPTER: PASS"
    )


if __name__ == "__main__":
    main()
