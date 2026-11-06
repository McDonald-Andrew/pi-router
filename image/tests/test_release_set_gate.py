from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import lzma
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


CERTIFICATION_DIR = Path(__file__).resolve().parents[1] / "certification"
sys.path.insert(0, str(CERTIFICATION_DIR))

import validate_receipt  # noqa: E402
import validate_release_set  # noqa: E402


class ReleaseSetGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.version = "1.2.3-rc.1"
        self.release_name = f"pi-router-v{self.version}"
        self.image_stem = f"{self.release_name}-pi5-arm64"
        self.router_commit = "1" * 40
        self.base_image_filename = (
            "2026-10-06-raspios-trixie-arm64.img.xz"
        )
        self.base_image_sha256 = "a" * 64

        self.archive = self.root / f"{self.release_name}.tar.gz"
        self.archive.write_bytes(b"router-release-archive-fixture")
        self._write_hash_only(
            self.root / f"{self.release_name}.sha256.txt", self.archive
        )

        self.installer = self.root / "pi-router-installer.exe"
        self.installer.write_bytes(b"windows-installer-fixture")
        self._write_hash_only(
            self.root / "pi-router-installer.sha256.txt", self.installer
        )
        (self.root / "THIRD-PARTY-NOTICES.txt").write_text(
            "fixture notices\n", encoding="ascii", newline=""
        )

        self.image = self.root / f"{self.image_stem}.img.xz"
        self.extracted = b"pi-router-disk-image\0" * 4096
        with lzma.open(self.image, "wb", format=lzma.FORMAT_XZ) as stream:
            stream.write(self.extracted)
        self.image_sha256 = hashlib.sha256(self.image.read_bytes()).hexdigest()
        (self.root / f"{self.image.name}.sha256").write_text(
            f"{self.image_sha256}  {self.image.name}\n",
            encoding="ascii",
            newline="",
        )

        self.manifest = self.root / f"{self.image_stem}.manifest.txt"
        self.manifest_values = {
            "certification_status": "release",
            "image_name": self.image_stem,
            "router_version": self.version,
            "router_source_commit": self.router_commit,
            "image_source_commit": self.router_commit,
            "router_release_sha256": self._hash(self.archive),
            "base_image_filename": self.base_image_filename,
            "base_image_sha256": self.base_image_sha256,
            "compressed_sha256": self.image_sha256,
            "compressed_size": str(self.image.stat().st_size),
            "github_release_asset_limit_bytes": "2147483648",
            "image_sha256": hashlib.sha256(self.extracted).hexdigest(),
            "image_size": str(len(self.extracted)),
        }
        self._write_manifest()

        self.imager_manifest = self.root / f"{self.image_stem}.rpi-imager-manifest"
        self.imager_values = {
            "os_list": [
                {
                    "name": f"Pi Router {self.version}",
                    "description": "Raspberry Pi OS Desktop configured as a Pi Router",
                    "icon": "https://example.test/icon.png",
                    "url": (
                        "https://github.com/example/pi-router/releases/download/"
                        f"v{self.version}/{self.image.name}"
                    ),
                    "extract_size": len(self.extracted),
                    "extract_sha256": hashlib.sha256(self.extracted).hexdigest(),
                    "image_download_size": self.image.stat().st_size,
                    "image_download_sha256": self.image_sha256,
                    "release_date": "2026-11-06",
                    "init_format": "cloudinit-rpi",
                    "devices": ["pi5-64bit"],
                    "capabilities": ["rpi_connect", "user_required"],
                }
            ]
        }
        self._write_imager_manifest()

        self.receipt = self.root / f"{self.image_stem}.certification.txt"
        self.receipt_values = {
            "format": validate_receipt.RECEIPT_FORMAT,
            "result": "PASS",
            "router_version": self.version,
            "router_source_commit": self.router_commit,
            "base_image_filename": self.base_image_filename,
            "base_image_sha256": self.base_image_sha256,
            "compressed_image_filename": self.image.name,
            "compressed_image_sha256": self.image_sha256,
            "compressed_image_size": str(self.image.stat().st_size),
            "extracted_image_sha256": hashlib.sha256(self.extracted).hexdigest(),
            "extracted_image_size": str(len(self.extracted)),
            "manifest_filename": self.manifest.name,
            "manifest_sha256": self._hash(self.manifest),
            "pi_model": "Raspberry Pi 5 Model B",
            "waveshare_pci_id": "10ec:8168",
            "identity_uniqueness": "PASS",
            "hardware_matrix": "PASS",
            "power_interruption_matrix": "PASS",
            "credential_residue": "PASS",
            "completed_utc": "2026-11-05T12:34:56Z",
            "tester_label": "release-lab",
        }
        self._write_receipt()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _write_hash_only(self, sidecar: Path, target: Path) -> None:
        sidecar.write_text(
            f"{self._hash(target)}\n", encoding="ascii", newline=""
        )

    def _write_manifest(self) -> None:
        self.manifest.write_text(
            "".join(
                f"{key}={self.manifest_values[key]}\n"
                for key in validate_receipt.MANIFEST_FIELDS
            ),
            encoding="ascii",
            newline="",
        )

    def _write_imager_manifest(self) -> None:
        self.imager_manifest.write_text(
            json.dumps(self.imager_values, indent=2) + "\n",
            encoding="utf-8",
            newline="",
        )

    def _write_receipt(self) -> None:
        self.receipt.write_text(
            "".join(
                f"{key}={self.receipt_values[key]}\n"
                for key in validate_receipt.RECEIPT_FIELDS
            ),
            encoding="ascii",
            newline="",
        )

    def _refresh_receipt_manifest_hash(self) -> None:
        self.receipt_values["manifest_sha256"] = self._hash(self.manifest)
        self._write_receipt()

    def assert_rejected(self, text: str) -> None:
        with self.assertRaises(
            validate_release_set.ReleaseSetValidationError
        ) as raised:
            validate_release_set.validate_release_set(
                self.version,
                self.router_commit,
                self.root,
            )
        self.assertIn(text, str(raised.exception))

    def test_valid_complete_release_set_passes(self) -> None:
        result = validate_release_set.validate_release_set(
            self.version,
            self.router_commit,
            self.root,
        )

        self.assertEqual(len(result), 10)
        self.assertEqual(result[self.archive.name], self._hash(self.archive))
        self.assertEqual(result[self.image.name], self.image_sha256)

    def test_missing_and_unexpected_entries_fail_closed(self) -> None:
        installer_checksum = self.root / "pi-router-installer.sha256.txt"
        installer_checksum.unlink()
        self.assert_rejected("missing asset: pi-router-installer.sha256.txt")

        self._write_hash_only(installer_checksum, self.installer)
        (self.root / "unexpected.txt").write_text("no\n", encoding="ascii")
        self.assert_rejected("unexpected entry: unexpected.txt")

    def test_nonregular_asset_is_rejected(self) -> None:
        notices = self.root / "THIRD-PARTY-NOTICES.txt"
        notices.unlink()
        notices.mkdir()

        self.assert_rejected("must be a regular file")

    def test_each_checksum_contract_is_enforced(self) -> None:
        cases = (
            (f"{self.release_name}.sha256.txt", "0" * 64 + "\n", "archive checksum"),
            ("pi-router-installer.sha256.txt", "0" * 64 + "\n", "installer checksum"),
            (
                f"{self.image.name}.sha256",
                "0" * 64 + f"  {self.image.name}\n",
                "image checksum",
            ),
        )
        originals = {
            name: (self.root / name).read_bytes() for name, _data, _text in cases
        }
        for name, content, expected in cases:
            with self.subTest(name=name):
                path = self.root / name
                path.write_text(content, encoding="ascii", newline="")
                self.assert_rejected(expected)
                path.write_bytes(originals[name])

    def test_image_sidecar_requires_canonical_filename_record(self) -> None:
        sidecar = self.root / f"{self.image.name}.sha256"
        sidecar.write_text(
            f"{self.image_sha256}\n", encoding="ascii", newline=""
        )

        self.assert_rejected(f"does not name {self.image.name}")

    def test_manifest_must_bind_the_source_archive(self) -> None:
        self.manifest_values["router_release_sha256"] = "3" * 64
        self._write_manifest()
        self._refresh_receipt_manifest_hash()

        self.assert_rejected("archive does not match the certified image manifest")

    def test_imager_manifest_must_bind_the_release_image(self) -> None:
        entry = self.imager_values["os_list"][0]
        entry["url"] = (
            "https://github.com/example/pi-router/releases/download/"
            f"v{self.version}/different.img.xz"
        )
        self._write_imager_manifest()
        self.assert_rejected("does not target the certified release image")

        entry["url"] = (
            "https://github.com/example/pi-router/releases/download/"
            f"v{self.version}/{self.image.name}"
        )
        entry["image_download_sha256"] = "3" * 64
        self._write_imager_manifest()
        self.assert_rejected("image_download_sha256")

    def test_receipt_failure_is_wrapped_without_bypassing_validation(self) -> None:
        self.receipt_values["hardware_matrix"] = "FAIL"
        self._write_receipt()

        self.assert_rejected("certification receipt is invalid")

    def test_certified_source_commit_must_match_release_target(self) -> None:
        with self.assertRaisesRegex(
            validate_release_set.ReleaseSetValidationError,
            "certified source commit does not match the release target",
        ):
            validate_release_set.validate_release_set(
                self.version,
                "3" * 40,
                self.root,
            )

    def test_version_and_asset_size_are_bounded(self) -> None:
        with self.assertRaisesRegex(
            validate_release_set.ReleaseSetValidationError, "version must match"
        ):
            validate_release_set.validate_release_set(
                "../1.2.3",
                self.router_commit,
                self.root,
            )

        for malformed_commit in ("1" * 39, "A" * 40):
            with self.subTest(expected_source_commit=malformed_commit):
                with self.assertRaisesRegex(
                    validate_release_set.ReleaseSetValidationError,
                    "expected source commit must be one full lowercase Git commit",
                ):
                    validate_release_set.validate_release_set(
                        self.version,
                        malformed_commit,
                        self.root,
                    )

        with mock.patch.object(
            validate_release_set,
            "GITHUB_RELEASE_ASSET_LIMIT_BYTES",
            self.archive.stat().st_size,
        ):
            self.assert_rejected("GitHub release asset limit")

    def test_cli_has_clean_pass_and_failure_output(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            success = validate_release_set.main(
                [self.version, self.router_commit, str(self.root)]
            )
        self.assertEqual(success, 0)
        self.assertIn("release set: PASS (10 assets", output.getvalue())
        self.assertEqual(errors.getvalue(), "")

        self.receipt_values["result"] = "FAIL"
        self._write_receipt()
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            failure = validate_release_set.main(
                [self.version, self.router_commit, str(self.root)]
            )
        self.assertEqual(failure, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("release set: FAIL", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
