from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse


IMAGE_DIR = Path(__file__).resolve().parents[1]
BASE_IMAGE_LOCK = IMAGE_DIR / "base-image.env"
MODULE_PATH = IMAGE_DIR / "generate-imager-manifest.py"
SPEC = importlib.util.spec_from_file_location("generate_imager_manifest", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
manifest_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest_module)

OFFICIAL_ENTRY = IMAGE_DIR / "imager" / "official-os-entry.json"
DEVICE_ENTRY = IMAGE_DIR / "imager" / "official-pi5-device.json"
SCHEMA = IMAGE_DIR / "imager" / "os-list-schema.json"
EXTRACTED_SHA256 = "a" * 64


def base_lock() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in BASE_IMAGE_LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key in values:
            raise AssertionError(f"invalid base lock line: {raw!r}")
        values[key] = value
    return values


class BaseImageLockTests(unittest.TestCase):
    def test_lock_has_the_complete_immutable_identity(self) -> None:
        values = base_lock()

        self.assertEqual(
            set(values),
            {
                "BASE_IMAGE_RELEASE_DATE",
                "BASE_IMAGE_URL",
                "BASE_IMAGE_COMPRESSED_SIZE",
                "BASE_IMAGE_COMPRESSED_SHA256",
                "BASE_IMAGE_EXTRACTED_SIZE",
                "BASE_IMAGE_EXTRACTED_SHA256",
            },
        )
        self.assertRegex(values["BASE_IMAGE_RELEASE_DATE"], r"^\d{4}-\d{2}-\d{2}$")
        parsed = urlparse(values["BASE_IMAGE_URL"])
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "downloads.raspberrypi.com")
        self.assertRegex(
            Path(parsed.path).name, r"^[0-9A-Za-z][0-9A-Za-z._-]*\.img\.xz$"
        )

        for key in (
            "BASE_IMAGE_COMPRESSED_SHA256",
            "BASE_IMAGE_EXTRACTED_SHA256",
        ):
            self.assertRegex(values[key], r"^[0-9a-f]{64}$")
        for key in ("BASE_IMAGE_COMPRESSED_SIZE", "BASE_IMAGE_EXTRACTED_SIZE"):
            self.assertRegex(values[key], r"^[1-9][0-9]*$")
            self.assertGreater(int(values[key]), 0)

    def test_lock_matches_the_reviewed_official_raspberry_pi_os_release(self) -> None:
        values = base_lock()

        self.assertEqual(values["BASE_IMAGE_RELEASE_DATE"], "2026-10-06")
        self.assertEqual(values["BASE_IMAGE_COMPRESSED_SIZE"], "1364720984")
        self.assertEqual(
            values["BASE_IMAGE_COMPRESSED_SHA256"],
            "976766570bee2aaf3047dd78649cfe82abb8fe9687b7f15030cd533ba4e9f7a9",
        )
        self.assertEqual(values["BASE_IMAGE_EXTRACTED_SIZE"], "6744440832")
        self.assertEqual(
            values["BASE_IMAGE_EXTRACTED_SHA256"],
            "2e0024bca282503a5cb6755500cbd7998cfc141c8f452808b59f9178623d2f0d",
        )

    def test_lock_is_plain_data_not_shell_programming(self) -> None:
        assignments = "\n".join(
            line
            for line in BASE_IMAGE_LOCK.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        )

        self.assertNotRegex(assignments, r"[`;$()]|\\\n")
        for value in base_lock().values():
            self.assertNotRegex(value, r"\s")


class PinnedOfficialInputTests(unittest.TestCase):
    def test_official_entry_matches_the_pinned_base_image(self) -> None:
        entry = json.loads(OFFICIAL_ENTRY.read_text(encoding="utf-8"))
        lock = base_lock()

        self.assertEqual(
            Path(urlparse(entry["url"]).path).name,
            Path(urlparse(lock["BASE_IMAGE_URL"]).path).name,
        )
        self.assertEqual(entry["release_date"], lock["BASE_IMAGE_RELEASE_DATE"])
        self.assertEqual(entry["extract_size"], int(lock["BASE_IMAGE_EXTRACTED_SIZE"]))
        self.assertEqual(entry["extract_sha256"], lock["BASE_IMAGE_EXTRACTED_SHA256"])
        self.assertEqual(
            entry["image_download_size"], int(lock["BASE_IMAGE_COMPRESSED_SIZE"])
        )
        self.assertNotIn("image_download_sha256", entry)
        self.assertEqual(entry["init_format"], "cloudinit-rpi")
        self.assertEqual(
            entry["devices"], ["pi5-64bit", "pi4-64bit", "pi3-64bit"]
        )

    def test_pinned_schema_has_the_reviewed_digest(self) -> None:
        self.assertEqual(
            hashlib.sha256(SCHEMA.read_bytes()).hexdigest(),
            "672390bc25c98d191ac43875b2ae85d5b6403ca7afce3a854e09d976277686b9",
        )


class ManifestGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = self.root / "pi-router-v1.2.3-pi5-arm64-development.img.xz"
        self.image.write_bytes(b"pi-router-compressed-image-fixture")
        self.compressed_sha256 = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.output = self.root / "pi-router-v1.2.3-pi5-arm64-development.rpi-imager-manifest"

    def arguments(self) -> list[str]:
        return [
            "--image",
            str(self.image),
            "--output",
            str(self.output),
            "--version",
            "1.2.3",
            "--profile",
            "development",
            "--release-date",
            "2026-11-06",
            "--compressed-size",
            str(self.image.stat().st_size),
            "--compressed-sha256",
            self.compressed_sha256,
            "--extracted-size",
            "4096",
            "--extracted-sha256",
            EXTRACTED_SHA256,
            "--imager-host-os",
            "linux",
        ]

    def test_generated_manifest_passes_schema_and_approved_diff(self) -> None:
        self.assertEqual(manifest_module.main(self.arguments()), 0)
        document = json.loads(self.output.read_text(encoding="utf-8"))
        entry = document["os_list"][0]

        self.assertEqual(entry["init_format"], "cloudinit-rpi")
        self.assertEqual(entry["devices"], ["pi5-64bit"])
        self.assertEqual(entry["capabilities"], ["rpi_connect", "user_required"])
        self.assertEqual(entry["image_download_sha256"], self.compressed_sha256)
        self.assertEqual(entry["extract_sha256"], EXTRACTED_SHA256)
        self.assertEqual(document["imager"]["devices"][0]["name"], "Raspberry Pi 5")
        self.assertEqual(entry["url"], self.image.resolve().as_uri())

    def test_release_manifest_uses_explicit_https_image_url(self) -> None:
        image_url = (
            "https://github.com/example/pi-router/releases/download/v1.2.3/"
            f"{self.image.name}"
        )
        arguments = self.arguments() + ["--image-url", image_url]

        self.assertEqual(manifest_module.main(arguments), 0)
        document = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(document["os_list"][0]["url"], image_url)

    def test_wrong_image_digest_fails_without_output(self) -> None:
        arguments = self.arguments()
        digest_index = arguments.index("--compressed-sha256") + 1
        arguments[digest_index] = "0" * 64

        self.assertEqual(manifest_module.main(arguments), 1)
        self.assertFalse(self.output.exists())

    def test_existing_output_is_not_overwritten(self) -> None:
        self.output.write_text("preserve\n", encoding="ascii")

        self.assertEqual(manifest_module.main(self.arguments()), 1)
        self.assertEqual(self.output.read_text(encoding="ascii"), "preserve\n")

    def test_unapproved_change_is_rejected(self) -> None:
        official = manifest_module.load_json_object(OFFICIAL_ENTRY)
        device = manifest_module.load_json_object(DEVICE_ENTRY)
        generated = manifest_module.build_manifest(
            official=official,
            device=device,
            version="1.2.3",
            profile="release",
            release_date="2026-11-06",
            image_url="https://example.test/pi-router.img.xz",
            compressed_size=100,
            compressed_sha256="b" * 64,
            extracted_size=200,
            extracted_sha256="c" * 64,
        )["os_list"][0]
        generated["init_format"] = "none"

        with self.assertRaises(manifest_module.ManifestError):
            manifest_module.validate_approved_differences(official, generated)

    def test_wsl_path_becomes_a_windows_file_uri(self) -> None:
        self.assertEqual(
            manifest_module.image_uri(
                Path("/mnt/c/Users/Test User/Downloads/router.img.xz"),
                host_os="windows",
            ),
            "file:///C:/Users/Test%20User/Downloads/router.img.xz",
        )


if __name__ == "__main__":
    unittest.main()
