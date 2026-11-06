#!/usr/bin/env python3
"""Generate one local Raspberry Pi Imager manifest for a Pi Router image."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlparse

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - exercised on a misconfigured host.
    raise SystemExit(
        "generate-imager-manifest: python3-jsonschema is required"
    ) from exc


SCRIPT_DIR: Final = Path(__file__).resolve().parent
DEFAULT_OFFICIAL_ENTRY: Final = SCRIPT_DIR / "imager" / "official-os-entry.json"
DEFAULT_DEVICE_ENTRY: Final = SCRIPT_DIR / "imager" / "official-pi5-device.json"
DEFAULT_SCHEMA: Final = SCRIPT_DIR / "imager" / "os-list-schema.json"
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
DATE_RE: Final = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}\Z")
VERSION_RE: Final = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?\Z")
WSL_PATH_RE: Final = re.compile(r"/mnt/([A-Za-z])(?:/(.*))?\Z")
ALLOWED_CHANGED_FIELDS: Final = frozenset(
    {
        "name",
        "description",
        "url",
        "extract_size",
        "extract_sha256",
        "image_download_size",
        "release_date",
        "devices",
        "capabilities",
    }
)
ALLOWED_ADDED_FIELDS: Final = frozenset({"image_download_sha256"})


class ManifestError(ValueError):
    """The pinned input or generated manifest violated its contract."""


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not path.is_file() or metadata.st_size > 1024 * 1024:
            raise ManifestError(f"unsafe JSON input: {path}")
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read JSON input: {path}") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"JSON input is not an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ManifestError(f"cannot hash image: {path}") from exc
    return digest.hexdigest()


def validate_sha256(value: str, label: str) -> str:
    normalized = value.lower()
    if SHA256_RE.fullmatch(normalized) is None:
        raise ManifestError(f"invalid {label}")
    return normalized


def image_uri(path: Path, host_os: str = "auto") -> str:
    absolute = path.expanduser().resolve(strict=False)
    selected = host_os
    if selected == "auto":
        selected = "windows" if os.environ.get("WSL_DISTRO_NAME") else "linux"
    if selected == "linux":
        return absolute.as_uri()
    if selected != "windows":
        raise ManifestError(f"unsupported Imager host OS: {selected}")

    match = WSL_PATH_RE.fullmatch(absolute.as_posix())
    if match is None:
        raise ManifestError(
            "Windows Imager output must be under a /mnt/<drive>/ WSL path"
        )
    drive = match.group(1).upper()
    remainder = quote(match.group(2) or "", safe="/")
    return f"file:///{drive}:/{remainder}"


def validate_source_inputs(
    official: dict[str, Any], device: dict[str, Any]
) -> None:
    required_os = {
        "name",
        "description",
        "icon",
        "url",
        "extract_size",
        "extract_sha256",
        "image_download_size",
        "release_date",
        "init_format",
        "devices",
        "capabilities",
    }
    if set(official) != required_os:
        raise ManifestError("pinned official OS entry fields changed")
    if official["init_format"] in {"", "none"}:
        raise ManifestError("official entry does not support OS customization")
    if official["devices"] != ["pi5-64bit", "pi4-64bit", "pi3-64bit"]:
        raise ManifestError("pinned official OS device list changed")
    if not isinstance(official["capabilities"], list):
        raise ManifestError("pinned official capabilities are invalid")
    if urlparse(str(official["url"])).scheme != "https":
        raise ManifestError("pinned official image URL is not HTTPS")
    validate_sha256(str(official["extract_sha256"]), "official extract SHA-256")

    required_device = {
        "name",
        "tags",
        "icon",
        "description",
        "matching_type",
        "capabilities",
    }
    if set(device) != required_device:
        raise ManifestError("pinned official Pi 5 device fields changed")
    if "pi5-64bit" not in device["tags"]:
        raise ManifestError("pinned device does not include pi5-64bit")


def validate_approved_differences(
    official: dict[str, Any], generated: dict[str, Any]
) -> None:
    expected_keys = set(official) | ALLOWED_ADDED_FIELDS
    if set(generated) != expected_keys:
        raise ManifestError("generated OS entry has unapproved fields")
    for key, original_value in official.items():
        if key not in ALLOWED_CHANGED_FIELDS and generated[key] != original_value:
            raise ManifestError(f"unapproved official-field change: {key}")
    if generated["devices"] != ["pi5-64bit"]:
        raise ManifestError("generated manifest is not restricted to Pi 5 64-bit")
    expected_capabilities = sorted(set(official["capabilities"]) | {"user_required"})
    if generated["capabilities"] != expected_capabilities:
        raise ManifestError("generated capabilities differ from the approved set")
    validate_sha256(
        str(generated["image_download_sha256"]), "image download SHA-256"
    )


def build_manifest(
    *,
    official: dict[str, Any],
    device: dict[str, Any],
    version: str,
    profile: str,
    release_date: str,
    image_url: str,
    compressed_size: int,
    compressed_sha256: str,
    extracted_size: int,
    extracted_sha256: str,
) -> dict[str, Any]:
    validate_source_inputs(official, device)
    if VERSION_RE.fullmatch(version) is None:
        raise ManifestError("invalid Pi Router version")
    if profile not in {"development", "release"}:
        raise ManifestError("invalid image profile")
    if DATE_RE.fullmatch(release_date) is None:
        raise ManifestError("invalid release date")
    parsed_url = urlparse(image_url)
    if parsed_url.scheme not in {"file", "https"}:
        raise ManifestError("image URL must use file or HTTPS")
    if compressed_size <= 0 or extracted_size <= 0:
        raise ManifestError("image sizes must be positive")

    display_suffix = " development" if profile == "development" else ""
    entry = dict(official)
    entry.update(
        {
            "name": f"Pi Router {version}{display_suffix}",
            "description": "Raspberry Pi OS Desktop configured as a Pi Router",
            "url": image_url,
            "extract_size": extracted_size,
            "extract_sha256": validate_sha256(
                extracted_sha256, "extracted image SHA-256"
            ),
            "image_download_size": compressed_size,
            "image_download_sha256": validate_sha256(
                compressed_sha256, "compressed image SHA-256"
            ),
            "release_date": release_date,
            "devices": ["pi5-64bit"],
            "capabilities": sorted(
                set(official["capabilities"]) | {"user_required"}
            ),
        }
    )
    validate_approved_differences(official, entry)
    return {"imager": {"devices": [device]}, "os_list": [entry]}


def validate_schema(manifest: dict[str, Any], schema: dict[str, Any]) -> None:
    try:
        jsonschema.Draft7Validator.check_schema(schema)
        jsonschema.Draft7Validator(schema).validate(manifest)
    except jsonschema.exceptions.SchemaError as exc:
        raise ManifestError(f"pinned Imager schema is invalid: {exc.message}") from exc
    except jsonschema.exceptions.ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "root"
        raise ManifestError(
            f"generated manifest violates official schema at {location}: {exc.message}"
        ) from exc


def write_new_json(path: Path, value: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise ManifestError(f"refusing to overwrite manifest: {path}")
    payload = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o644,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ManifestError(f"cannot create manifest: {path}") from exc


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--published-image-path", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--profile", required=True, choices=("development", "release")
    )
    parser.add_argument("--release-date", required=True)
    parser.add_argument("--compressed-size", required=True, type=int)
    parser.add_argument("--compressed-sha256", required=True)
    parser.add_argument("--extracted-size", required=True, type=int)
    parser.add_argument("--extracted-sha256", required=True)
    parser.add_argument("--image-url")
    parser.add_argument(
        "--imager-host-os", choices=("auto", "linux", "windows"), default="auto"
    )
    parser.add_argument("--official-entry", type=Path, default=DEFAULT_OFFICIAL_ENTRY)
    parser.add_argument("--device-entry", type=Path, default=DEFAULT_DEVICE_ENTRY)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    try:
        image_source = arguments.image
        image_metadata = image_source.lstat()
        if image_source.is_symlink() or not image_source.is_file():
            raise ManifestError("image is not a regular file")
        image_path = image_source.resolve(strict=True)
        if image_metadata.st_size != arguments.compressed_size:
            raise ManifestError("compressed image size does not match")
        actual_sha256 = sha256_file(image_path)
        expected_sha256 = validate_sha256(
            arguments.compressed_sha256, "compressed image SHA-256"
        )
        if actual_sha256 != expected_sha256:
            raise ManifestError("compressed image SHA-256 does not match")

        official = load_json_object(arguments.official_entry)
        device = load_json_object(arguments.device_entry)
        schema = load_json_object(arguments.schema)
        published_image_path = arguments.published_image_path or image_path
        image_url = arguments.image_url or image_uri(
            published_image_path,
            host_os=arguments.imager_host_os,
        )
        manifest = build_manifest(
            official=official,
            device=device,
            version=arguments.version,
            profile=arguments.profile,
            release_date=arguments.release_date,
            image_url=image_url,
            compressed_size=arguments.compressed_size,
            compressed_sha256=expected_sha256,
            extracted_size=arguments.extracted_size,
            extracted_sha256=arguments.extracted_sha256,
        )
        validate_schema(manifest, schema)
        write_new_json(arguments.output, manifest)
    except (ManifestError, OSError) as exc:
        print(f"generate-imager-manifest: ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
