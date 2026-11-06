#!/usr/bin/env python3
"""Validate a Pi Router hardware-certification receipt.

The receipt is deliberately small and rigid.  It records assertions, not raw
hardware identifiers: serial numbers, MAC addresses, SSH host keys, and other
device-private identities have no field in the format.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import lzma
import os
from pathlib import Path
import re
import stat
import sys
from typing import Mapping, Sequence


RECEIPT_FORMAT = "pi-router-certification-receipt-v2"
RECEIPT_FIELDS = (
    "format",
    "result",
    "router_version",
    "router_source_commit",
    "base_image_filename",
    "base_image_sha256",
    "compressed_image_filename",
    "compressed_image_sha256",
    "compressed_image_size",
    "extracted_image_sha256",
    "extracted_image_size",
    "manifest_filename",
    "manifest_sha256",
    "pi_model",
    "waveshare_pci_id",
    "identity_uniqueness",
    "hardware_matrix",
    "power_interruption_matrix",
    "credential_residue",
    "completed_utc",
    "tester_label",
)
MANIFEST_FIELDS = (
    "certification_status",
    "image_name",
    "router_version",
    "router_source_commit",
    "image_source_commit",
    "router_release_sha256",
    "base_image_filename",
    "base_image_sha256",
    "compressed_sha256",
    "compressed_size",
    "github_release_asset_limit_bytes",
    "image_sha256",
    "image_size",
)

MAX_RECEIPT_BYTES = 4096
MAX_MANIFEST_BYTES = 1024 * 1024
GITHUB_RELEASE_ASSET_LIMIT_BYTES = 2_147_483_648
MAX_EXTRACTED_IMAGE_BYTES = 128 * 1024 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024

VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
BASE_IMAGE_FILENAME_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}-raspios-[a-z0-9-]+-arm64\.img\.xz\Z"
)
POSITIVE_INTEGER_RE = re.compile(r"[1-9][0-9]*\Z")
UTC_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
TESTER_LABEL_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_. -]{0,62}[A-Za-z0-9])?\Z"
)
PRIVATE_LABEL_TERM_RE = re.compile(
    r"(?:\A|[_. -])"
    r"(?:serial|mac(?:address)?|uuid|hostkey|host-key|fingerprint)"
    r"(?:\Z|[_. -])",
    re.IGNORECASE,
)
PRIVATE_IDENTITY_SHAPE_RE = re.compile(
    r"(?:[0-9a-fA-F]{12,})"
    r"|(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"|(?:(?:[0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2})"
)


class ReceiptValidationError(ValueError):
    """Raised when certification evidence fails closed."""


def _fail(message: str) -> None:
    raise ReceiptValidationError(message)


def _regular_file_stat(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        _fail(f"cannot inspect {label}: {exc}")
    if stat.S_ISLNK(metadata.st_mode):
        _fail(f"{label} must not be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        _fail(f"{label} must be a regular file")
    return metadata


def _read_bounded_ascii(path: Path, label: str, limit: int) -> tuple[bytes, str]:
    metadata = _regular_file_stat(path, label)
    if metadata.st_size == 0:
        _fail(f"{label} is empty")
    if metadata.st_size > limit:
        _fail(f"{label} exceeds the {limit}-byte limit")
    try:
        data = path.read_bytes()
    except OSError as exc:
        _fail(f"cannot read {label}: {exc}")
    if len(data) != metadata.st_size:
        _fail(f"{label} changed while it was read")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        _fail(f"{label} must contain ASCII only")
    return data, text


def _parse_receipt(path: Path) -> dict[str, str]:
    _data, text = _read_bounded_ascii(
        path, "certification receipt", MAX_RECEIPT_BYTES
    )
    if "\r" in text:
        _fail("certification receipt must use LF line endings")
    if not text.endswith("\n"):
        _fail("certification receipt must end with exactly one LF")

    lines = text[:-1].split("\n")
    if not lines or lines[-1] == "":
        _fail("certification receipt must end with exactly one LF")
    if len(lines) != len(RECEIPT_FIELDS):
        _fail(
            "certification receipt has the wrong number of fields; "
            "unknown, missing, or duplicate fields are forbidden"
        )

    values: dict[str, str] = {}
    for position, (line, expected_key) in enumerate(
        zip(lines, RECEIPT_FIELDS), start=1
    ):
        if "=" not in line:
            _fail(f"receipt line {position} is not key=value")
        key, value = line.split("=", 1)
        if key != expected_key:
            _fail(
                f"receipt line {position} must be {expected_key}=; "
                "fields must be complete and ordered"
            )
        if value == "":
            _fail(f"receipt field {key} must not be empty")
        values[key] = value

    if values["format"] != RECEIPT_FORMAT:
        _fail("unsupported certification receipt format")
    if values["result"] != "PASS":
        _fail("certification result is not PASS")
    if VERSION_RE.fullmatch(values["router_version"]) is None:
        _fail("router_version is not canonical")
    if COMMIT_RE.fullmatch(values["router_source_commit"]) is None:
        _fail("router_source_commit must be one full lowercase Git commit")
    if BASE_IMAGE_FILENAME_RE.fullmatch(values["base_image_filename"]) is None:
        _fail("base_image_filename is not a canonical official image basename")
    for key in (
        "base_image_sha256",
        "compressed_image_sha256",
        "extracted_image_sha256",
        "manifest_sha256",
    ):
        if SHA256_RE.fullmatch(values[key]) is None:
            _fail(f"{key} must be one lowercase SHA-256 value")
    for key in ("compressed_image_size", "extracted_image_size"):
        if POSITIVE_INTEGER_RE.fullmatch(values[key]) is None:
            _fail(f"{key} must be a canonical positive integer")

    if values["pi_model"] != "Raspberry Pi 5 Model B":
        _fail("pi_model does not assert Raspberry Pi 5 Model B")
    if values["waveshare_pci_id"] != "10ec:8168":
        _fail("waveshare_pci_id does not assert 10ec:8168")
    for key in (
        "identity_uniqueness",
        "hardware_matrix",
        "power_interruption_matrix",
        "credential_residue",
    ):
        if values[key] != "PASS":
            _fail(f"{key} is not PASS")

    completed = values["completed_utc"]
    if UTC_RE.fullmatch(completed) is None:
        _fail("completed_utc must be UTC with second precision")
    try:
        datetime.strptime(completed, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _fail("completed_utc is not a valid UTC timestamp")

    tester_label = values["tester_label"]
    if TESTER_LABEL_RE.fullmatch(tester_label) is None:
        _fail("tester_label must be 1-64 safe ASCII characters")
    if (
        PRIVATE_LABEL_TERM_RE.search(tester_label) is not None
        or PRIVATE_IDENTITY_SHAPE_RE.search(tester_label) is not None
    ):
        _fail("tester_label resembles a device-private identity")

    return values


def _parse_manifest(path: Path, expected_sha256: str) -> dict[str, str]:
    data, text = _read_bounded_ascii(path, "external manifest", MAX_MANIFEST_BYTES)
    manifest_sha256 = hashlib.sha256(data).hexdigest()
    if manifest_sha256 != expected_sha256:
        _fail("external manifest SHA-256 does not match the receipt")
    if "\r" in text:
        _fail("external manifest must use LF line endings")
    if not text.endswith("\n"):
        _fail("external manifest must end with LF")

    lines = text[:-1].split("\n")
    if len(lines) != len(MANIFEST_FIELDS):
        _fail(
            "external manifest has the wrong number of metadata fields; "
            "unknown, missing, or duplicate fields are forbidden"
        )

    values: dict[str, str] = {}
    for position, (line, expected_key) in enumerate(
        zip(lines, MANIFEST_FIELDS), start=1
    ):
        if "=" not in line:
            _fail(f"external manifest line {position} is not key=value")
        key, value = line.split("=", 1)
        if key != expected_key:
            _fail(
                f"external manifest line {position} must be {expected_key}=; "
                "metadata must be complete and ordered"
            )
        if value == "":
            _fail(f"external manifest field {key} must not be empty")
        values[key] = value

    return values


def _canonical_positive_integer(value: str, label: str) -> int:
    if POSITIVE_INTEGER_RE.fullmatch(value) is None:
        _fail(f"{label} must be a canonical positive integer")
    return int(value)


def _validate_manifest(
    manifest: Mapping[str, str], receipt: Mapping[str, str]
) -> None:
    version = receipt["router_version"]
    image_name = f"pi-router-v{version}-pi5-arm64"

    expected_literals = {
        "certification_status": "release",
        "image_name": image_name,
        "router_version": version,
        "router_source_commit": receipt["router_source_commit"],
        "image_source_commit": receipt["router_source_commit"],
        "base_image_filename": receipt["base_image_filename"],
        "base_image_sha256": receipt["base_image_sha256"],
        "compressed_sha256": receipt["compressed_image_sha256"],
        "compressed_size": receipt["compressed_image_size"],
        "github_release_asset_limit_bytes": str(
            GITHUB_RELEASE_ASSET_LIMIT_BYTES
        ),
        "image_sha256": receipt["extracted_image_sha256"],
        "image_size": receipt["extracted_image_size"],
    }
    for key, expected in expected_literals.items():
        if manifest[key] != expected:
            _fail(f"external manifest {key} does not match the receipt")
    if SHA256_RE.fullmatch(manifest["router_release_sha256"]) is None:
        _fail("external manifest router_release_sha256 is not canonical")

    compressed_size = _canonical_positive_integer(
        manifest["compressed_size"], "external manifest compressed_size"
    )
    image_size = _canonical_positive_integer(
        manifest["image_size"], "external manifest image_size"
    )
    if compressed_size >= GITHUB_RELEASE_ASSET_LIMIT_BYTES:
        _fail("compressed image is not below the GitHub release asset limit")
    if image_size > MAX_EXTRACTED_IMAGE_BYTES:
        _fail("declared extracted image exceeds the validation safety limit")


def _validate_filenames(
    receipt: Mapping[str, str], image_path: Path, manifest_path: Path
) -> None:
    version = receipt["router_version"]
    expected_image = f"pi-router-v{version}-pi5-arm64.img.xz"
    expected_manifest = f"pi-router-v{version}-pi5-arm64.manifest.txt"
    if receipt["compressed_image_filename"] != expected_image:
        _fail("receipt compressed image filename is not canonical")
    if receipt["manifest_filename"] != expected_manifest:
        _fail("receipt manifest filename is not canonical")
    if image_path.name != expected_image:
        _fail("provided compressed image basename does not match the receipt")
    if manifest_path.name != expected_manifest:
        _fail("provided manifest basename does not match the receipt")


def _hash_image(
    path: Path,
    declared_compressed_size: int,
    expected_compressed_sha256: str,
    declared_extracted_size: int,
) -> tuple[str, int, str, int]:
    metadata = _regular_file_stat(path, "compressed image")
    if metadata.st_size != declared_compressed_size:
        _fail("compressed image size does not match the receipt")
    if metadata.st_size >= GITHUB_RELEASE_ASSET_LIMIT_BYTES:
        _fail("compressed image is not below the GitHub release asset limit")

    compressed_hash = hashlib.sha256()
    extracted_hash = hashlib.sha256()
    compressed_size = 0
    extracted_size = 0

    try:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (
                opened.st_size != metadata.st_size
                or opened.st_mtime_ns != metadata.st_mtime_ns
            ):
                _fail("compressed image changed before validation")
            while chunk := source.read(CHUNK_BYTES):
                compressed_hash.update(chunk)
                compressed_size += len(chunk)

            if compressed_hash.hexdigest() != expected_compressed_sha256:
                _fail("compressed image SHA-256 does not match the receipt")

            source.seek(0)
            with lzma.LZMAFile(source, "rb") as extracted:
                while chunk := extracted.read(CHUNK_BYTES):
                    extracted_size += len(chunk)
                    if extracted_size > declared_extracted_size:
                        _fail("extracted image size exceeds the receipt")
                    if extracted_size > MAX_EXTRACTED_IMAGE_BYTES:
                        _fail("extracted image exceeds the validation safety limit")
                    extracted_hash.update(chunk)

            finished = os.fstat(source.fileno())
            if (
                finished.st_size != opened.st_size
                or finished.st_mtime_ns != opened.st_mtime_ns
            ):
                _fail("compressed image changed during validation")
    except ReceiptValidationError:
        raise
    except (EOFError, lzma.LZMAError, OSError) as exc:
        _fail(f"cannot stream the XZ image: {exc}")

    if compressed_size == 0 or extracted_size == 0:
        _fail("compressed and extracted images must be non-empty")
    return (
        compressed_hash.hexdigest(),
        compressed_size,
        extracted_hash.hexdigest(),
        extracted_size,
    )


def validate_certification_receipt(
    receipt_path: str | Path,
    image_path: str | Path,
    manifest_path: str | Path,
) -> Mapping[str, str]:
    """Validate and return an immutable-use certification receipt mapping."""

    receipt_file = Path(receipt_path)
    image_file = Path(image_path)
    manifest_file = Path(manifest_path)

    receipt = _parse_receipt(receipt_file)
    _validate_filenames(receipt, image_file, manifest_file)
    manifest = _parse_manifest(manifest_file, receipt["manifest_sha256"])
    _validate_manifest(manifest, receipt)

    expected_compressed_size = int(receipt["compressed_image_size"])
    expected_extracted_size = int(receipt["extracted_image_size"])
    (
        observed_compressed_sha256,
        observed_compressed_size,
        observed_extracted_sha256,
        observed_extracted_size,
    ) = _hash_image(
        image_file,
        expected_compressed_size,
        receipt["compressed_image_sha256"],
        expected_extracted_size,
    )

    if observed_compressed_sha256 != receipt["compressed_image_sha256"]:
        _fail("compressed image SHA-256 does not match the receipt")
    if observed_compressed_size != expected_compressed_size:
        _fail("compressed image size does not match the receipt")
    if observed_extracted_sha256 != receipt["extracted_image_sha256"]:
        _fail("extracted image SHA-256 does not match the receipt")
    if observed_extracted_size != expected_extracted_size:
        _fail("extracted image size does not match the receipt")

    return dict(receipt)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed validation of a Pi Router P11 certification receipt "
            "against an exact release image and manifest."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("receipt", help="path to the certification receipt")
    parser.add_argument("image", help="path to the release .img.xz")
    parser.add_argument("manifest", help="path to the external manifest")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        receipt = validate_certification_receipt(
            args.receipt, args.image, args.manifest
        )
    except ReceiptValidationError as exc:
        print(f"certification receipt: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "certification receipt: PASS "
        f"({receipt['compressed_image_filename']}, "
        f"completed {receipt['completed_utc']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
