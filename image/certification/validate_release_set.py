#!/usr/bin/env python3
"""Validate the exact asset set permitted at the P10 publish boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Mapping, Sequence
from urllib.parse import unquote, urlparse

import validate_receipt


GITHUB_RELEASE_ASSET_LIMIT_BYTES = 2_147_483_648
CHUNK_BYTES = 1024 * 1024
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class ReleaseSetValidationError(ValueError):
    """Raised when an assembled release set is unsafe to publish."""


def _fail(message: str) -> None:
    raise ReleaseSetValidationError(message)


def _expected_assets(version: str) -> tuple[str, ...]:
    release_name = f"pi-router-v{version}"
    image_name = f"{release_name}-pi5-arm64"
    return (
        f"{release_name}.tar.gz",
        f"{release_name}.sha256.txt",
        "pi-router-installer.exe",
        "pi-router-installer.sha256.txt",
        "THIRD-PARTY-NOTICES.txt",
        f"{image_name}.img.xz",
        f"{image_name}.img.xz.sha256",
        f"{image_name}.manifest.txt",
        f"{image_name}.rpi-imager-manifest",
        f"{image_name}.certification.txt",
    )


def _require_release_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        _fail(f"cannot inspect release directory: {exc}")
    if stat.S_ISLNK(metadata.st_mode):
        _fail("release directory must not be a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        _fail("release directory is not a directory")


def _inventory(path: Path, expected: tuple[str, ...]) -> Mapping[str, Path]:
    try:
        entries = {entry.name: entry for entry in path.iterdir()}
    except OSError as exc:
        _fail(f"cannot inventory release directory: {exc}")
    expected_names = set(expected)
    observed_names = set(entries)
    missing = sorted(expected_names - observed_names)
    unexpected = sorted(observed_names - expected_names)
    if missing:
        _fail(f"release set is missing asset: {missing[0]}")
    if unexpected:
        _fail(f"release set has unexpected entry: {unexpected[0]}")
    return {name: entries[name] for name in expected}


def _hash_release_file(path: Path, label: str) -> tuple[str, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        _fail(f"cannot inspect {label}: {exc}")
    if stat.S_ISLNK(metadata.st_mode):
        _fail(f"{label} must not be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        _fail(f"{label} must be a regular file")
    if metadata.st_size == 0:
        _fail(f"{label} is empty")
    if metadata.st_size >= GITHUB_RELEASE_ASSET_LIMIT_BYTES:
        _fail(f"{label} is not below the GitHub release asset limit")

    digest = hashlib.sha256()
    observed_size = 0
    try:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                _fail(f"{label} changed to a non-regular file")
            if (
                opened.st_size != metadata.st_size
                or opened.st_mtime_ns != metadata.st_mtime_ns
                or (
                    metadata.st_ino != 0
                    and opened.st_ino != 0
                    and opened.st_ino != metadata.st_ino
                )
            ):
                _fail(f"{label} changed before validation")
            while chunk := source.read(CHUNK_BYTES):
                digest.update(chunk)
                observed_size += len(chunk)
            finished = os.fstat(source.fileno())
            if (
                finished.st_size != opened.st_size
                or finished.st_mtime_ns != opened.st_mtime_ns
            ):
                _fail(f"{label} changed during validation")
    except ReleaseSetValidationError:
        raise
    except OSError as exc:
        _fail(f"cannot read {label}: {exc}")

    if observed_size != metadata.st_size:
        _fail(f"{label} size changed during validation")
    return digest.hexdigest(), observed_size


def _read_sidecar(path: Path, label: str, filename: str | None) -> str:
    digest, size = _hash_release_file(path, label)
    if size > 1024:
        _fail(f"{label} is too large")
    try:
        data = path.read_bytes()
        text = data.decode("ascii")
    except UnicodeDecodeError:
        _fail(f"{label} must contain ASCII only")
    except OSError as exc:
        _fail(f"cannot read {label}: {exc}")
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        _fail(f"{label} changed while it was read")

    ending = "\r\n" if text.endswith("\r\n") else "\n"
    if not text.endswith(ending) or text.count("\n") != 1:
        _fail(f"{label} must contain exactly one newline-terminated record")
    record = text[: -len(ending)]
    if "\r" in record:
        _fail(f"{label} has invalid line endings")
    if filename is None:
        value = record
    else:
        suffix = f"  {filename}"
        if not record.endswith(suffix):
            _fail(f"{label} does not name {filename}")
        value = record[: -len(suffix)]
    if SHA256_RE.fullmatch(value) is None:
        _fail(f"{label} must contain one lowercase SHA-256 value")
    return value


def _read_imager_manifest(path: Path, expected_digest: str) -> Mapping[str, object]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        _fail(f"cannot read Raspberry Pi Imager manifest: {exc}")
    if len(data) > 1024 * 1024:
        _fail("Raspberry Pi Imager manifest is too large")
    if hashlib.sha256(data).hexdigest() != expected_digest:
        _fail("Raspberry Pi Imager manifest changed while it was read")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                _fail(f"Raspberry Pi Imager manifest has duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"Raspberry Pi Imager manifest is invalid JSON: {exc}")
    if not isinstance(value, dict):
        _fail("Raspberry Pi Imager manifest root must be an object")
    return value


def validate_release_set(
    version: str,
    expected_source_commit: str,
    directory: str | Path,
) -> Mapping[str, str]:
    """Validate and return hashes for the exact publishable release set."""

    if VERSION_RE.fullmatch(version) is None:
        _fail("version must match X.Y.Z or X.Y.Z-prerelease")
    if COMMIT_RE.fullmatch(expected_source_commit) is None:
        _fail("expected source commit must be one full lowercase Git commit")

    root = Path(directory)
    _require_release_directory(root)
    expected = _expected_assets(version)
    assets = _inventory(root, expected)

    observed_hashes: dict[str, str] = {}
    for name in expected:
        observed_hashes[name], _size = _hash_release_file(
            assets[name], f"release asset {name}"
        )

    release_name = f"pi-router-v{version}"
    archive_name = f"{release_name}.tar.gz"
    archive_sidecar_name = f"{release_name}.sha256.txt"
    image_name = f"{release_name}-pi5-arm64.img.xz"
    image_sidecar_name = f"{image_name}.sha256"
    manifest_name = f"{release_name}-pi5-arm64.manifest.txt"
    imager_manifest_name = f"{release_name}-pi5-arm64.rpi-imager-manifest"
    receipt_name = f"{release_name}-pi5-arm64.certification.txt"

    archive_expected = _read_sidecar(
        assets[archive_sidecar_name], "router archive checksum", None
    )
    if observed_hashes[archive_name] != archive_expected:
        _fail("router archive checksum does not match")

    installer_expected = _read_sidecar(
        assets["pi-router-installer.sha256.txt"],
        "Windows installer checksum",
        None,
    )
    if observed_hashes["pi-router-installer.exe"] != installer_expected:
        _fail("Windows installer checksum does not match")

    image_expected = _read_sidecar(
        assets[image_sidecar_name], "compressed image checksum", image_name
    )
    if observed_hashes[image_name] != image_expected:
        _fail("compressed image checksum does not match")

    try:
        receipt = validate_receipt.validate_certification_receipt(
            assets[receipt_name], assets[image_name], assets[manifest_name]
        )
        manifest = validate_receipt._parse_manifest(
            assets[manifest_name], receipt["manifest_sha256"]
        )
    except validate_receipt.ReceiptValidationError as exc:
        _fail(f"certification receipt is invalid: {exc}")

    if receipt["router_version"] != version:
        _fail("certification receipt version does not match the release")
    if receipt["router_source_commit"] != expected_source_commit:
        _fail("certified source commit does not match the release target")
    if manifest["router_source_commit"] != expected_source_commit:
        _fail("manifest router source commit does not match the release target")
    if manifest["image_source_commit"] != expected_source_commit:
        _fail("manifest image source commit does not match the release target")
    if receipt["compressed_image_sha256"] != image_expected:
        _fail("image sidecar does not match the certification receipt")
    if manifest["router_release_sha256"] != archive_expected:
        _fail("router archive does not match the certified image manifest")

    imager_manifest = _read_imager_manifest(
        assets[imager_manifest_name], observed_hashes[imager_manifest_name]
    )
    os_list = imager_manifest.get("os_list")
    if not isinstance(os_list, list) or len(os_list) != 1:
        _fail("Raspberry Pi Imager manifest must contain exactly one OS entry")
    entry = os_list[0]
    if not isinstance(entry, dict):
        _fail("Raspberry Pi Imager OS entry must be an object")
    image_url = entry.get("url")
    if not isinstance(image_url, str):
        _fail("Raspberry Pi Imager image URL is missing")
    parsed_url = urlparse(image_url)
    expected_url_suffix = f"/releases/download/v{version}/{image_name}"
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "github.com"
        or not unquote(parsed_url.path).endswith(expected_url_suffix)
    ):
        _fail("Raspberry Pi Imager manifest does not target the certified release image")
    imager_checks = {
        "image_download_sha256": image_expected,
        "image_download_size": int(manifest["compressed_size"]),
        "extract_sha256": manifest["image_sha256"],
        "extract_size": int(manifest["image_size"]),
        "init_format": "cloudinit-rpi",
        "devices": ["pi5-64bit"],
    }
    for key, expected in imager_checks.items():
        if entry.get(key) != expected:
            _fail(f"Raspberry Pi Imager manifest mismatch: {key}")
    capabilities = entry.get("capabilities")
    if not isinstance(capabilities, list) or "user_required" not in capabilities:
        _fail("Raspberry Pi Imager manifest does not require OS account setup")

    return dict(observed_hashes)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed validation of the complete P10 release set.",
        allow_abbrev=False,
    )
    parser.add_argument("version", help="release version without the leading v")
    parser.add_argument(
        "expected_source_commit",
        help="full lowercase Git commit that the tag/release will target",
    )
    parser.add_argument("directory", help="directory containing only release assets")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        assets = validate_release_set(
            args.version,
            args.expected_source_commit,
            args.directory,
        )
    except ReleaseSetValidationError as exc:
        print(f"release set: FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"release set: PASS ({len(assets)} assets for v{args.version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
