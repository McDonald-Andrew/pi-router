from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import lzma
from pathlib import Path
import sys
import tempfile
import unittest


CERTIFICATION_DIR = Path(__file__).resolve().parents[1] / "certification"
sys.path.insert(0, str(CERTIFICATION_DIR))

import validate_receipt  # noqa: E402


class CertificationReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.version = "1.2.3-rc.1"
        self.image_name = f"pi-router-v{self.version}-pi5-arm64"
        self.image = self.root / f"{self.image_name}.img.xz"
        self.manifest = self.root / f"{self.image_name}.manifest.txt"
        self.receipt = self.root / "certification-receipt.txt"
        self.router_commit = "1" * 40
        self.base_image_filename = "2026-10-06-raspios-desktop-arm64.img.xz"
        self.base_image_sha256 = "a" * 64

        self.extracted = (b"pi-router-image-fixture\0" * 4096) + b"end"
        with lzma.open(self.image, "wb", format=lzma.FORMAT_XZ) as stream:
            stream.write(self.extracted)
        compressed = self.image.read_bytes()

        self.manifest_values = {
            "certification_status": "release",
            "image_name": self.image_name,
            "router_version": self.version,
            "router_source_commit": self.router_commit,
            "image_source_commit": self.router_commit,
            "router_release_sha256": "3" * 64,
            "base_image_filename": self.base_image_filename,
            "base_image_sha256": self.base_image_sha256,
            "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
            "compressed_size": str(len(compressed)),
            "github_release_asset_limit_bytes": "2147483648",
            "image_sha256": hashlib.sha256(self.extracted).hexdigest(),
            "image_size": str(len(self.extracted)),
        }
        self.write_manifest()

        self.receipt_values = {
            "format": validate_receipt.RECEIPT_FORMAT,
            "result": "PASS",
            "router_version": self.version,
            "router_source_commit": self.router_commit,
            "base_image_filename": self.base_image_filename,
            "base_image_sha256": self.base_image_sha256,
            "compressed_image_filename": self.image.name,
            "compressed_image_sha256": self.manifest_values[
                "compressed_sha256"
            ],
            "compressed_image_size": self.manifest_values["compressed_size"],
            "extracted_image_sha256": self.manifest_values["image_sha256"],
            "extracted_image_size": self.manifest_values["image_size"],
            "manifest_filename": self.manifest.name,
            "manifest_sha256": hashlib.sha256(
                self.manifest.read_bytes()
            ).hexdigest(),
            "pi_model": "Raspberry Pi 5 Model B",
            "waveshare_pci_id": "10ec:8168",
            "identity_uniqueness": "PASS",
            "hardware_matrix": "PASS",
            "power_interruption_matrix": "PASS",
            "credential_residue": "PASS",
            "completed_utc": "2026-11-05T12:34:56Z",
            "tester_label": "integration-lab-a",
        }
        self.write_receipt()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_manifest(self) -> None:
        content = "".join(
            f"{key}={self.manifest_values[key]}\n"
            for key in validate_receipt.MANIFEST_FIELDS
        )
        self.manifest.write_text(content, encoding="ascii", newline="")

    def write_receipt(self) -> None:
        content = "".join(
            f"{key}={self.receipt_values[key]}\n"
            for key in validate_receipt.RECEIPT_FIELDS
        )
        self.receipt.write_text(content, encoding="ascii", newline="")

    def refresh_manifest_hash(self) -> None:
        self.receipt_values["manifest_sha256"] = hashlib.sha256(
            self.manifest.read_bytes()
        ).hexdigest()
        self.write_receipt()

    def assert_rejected(self, message: str | None = None) -> None:
        with self.assertRaises(validate_receipt.ReceiptValidationError) as raised:
            validate_receipt.validate_certification_receipt(
                self.receipt, self.image, self.manifest
            )
        if message is not None:
            self.assertIn(message, str(raised.exception))

    def test_valid_receipt_binds_image_manifest_and_hardware_assertions(self) -> None:
        result = validate_receipt.validate_certification_receipt(
            self.receipt, self.image, self.manifest
        )

        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["router_source_commit"], self.router_commit)
        self.assertEqual(
            result["base_image_filename"], self.base_image_filename
        )
        self.assertEqual(result["base_image_sha256"], self.base_image_sha256)
        self.assertEqual(result["waveshare_pci_id"], "10ec:8168")

    def test_manifest_rejects_pi_gen_info_trailer(self) -> None:
        with self.manifest.open("a", encoding="ascii", newline="") as stream:
            stream.write("\n[pi-gen-info]\nIMG_NAME=fixture\n")
        self.refresh_manifest_hash()

        self.assert_rejected("wrong number of metadata fields")

    def test_base_image_filename_must_be_a_safe_official_basename(self) -> None:
        self.receipt_values["base_image_filename"] = "../base.img.xz"
        self.write_receipt()

        self.assert_rejected("base_image_filename")

    def test_base_image_sha256_must_be_lowercase(self) -> None:
        self.receipt_values[
            "base_image_sha256"
        ] = self.base_image_sha256.upper()
        self.write_receipt()

        self.assert_rejected("base_image_sha256")

    def test_manifest_must_bind_base_image_provenance(self) -> None:
        self.manifest_values["base_image_sha256"] = "4" * 64
        self.write_manifest()
        self.refresh_manifest_hash()

        self.assert_rejected("base_image_sha256")

    def test_compressed_image_tampering_is_rejected(self) -> None:
        data = bytearray(self.image.read_bytes())
        data[-1] ^= 1
        self.image.write_bytes(data)

        self.assert_rejected("compressed image SHA-256")

    def test_extracted_hash_mismatch_is_rejected(self) -> None:
        self.receipt_values["extracted_image_sha256"] = "4" * 64
        self.manifest_values["image_sha256"] = "4" * 64
        self.write_manifest()
        self.refresh_manifest_hash()

        self.assert_rejected("extracted image SHA-256")

    def test_manifest_tampering_is_rejected(self) -> None:
        self.manifest.write_bytes(self.manifest.read_bytes() + b"\n")

        self.assert_rejected("manifest SHA-256")

    def test_release_status_is_required(self) -> None:
        self.manifest_values["certification_status"] = "certified"
        self.write_manifest()
        self.refresh_manifest_hash()

        self.assert_rejected("certification_status")

    def test_image_source_commit_is_required_and_must_match(self) -> None:
        self.manifest_values["image_source_commit"] = "5" * 40
        self.write_manifest()
        self.refresh_manifest_hash()

        self.assert_rejected("image_source_commit")

    def test_canonical_image_and_manifest_basenames_are_required(self) -> None:
        wrong_image = self.root / "renamed.img.xz"
        self.image.rename(wrong_image)

        with self.assertRaisesRegex(
            validate_receipt.ReceiptValidationError, "compressed image basename"
        ):
            validate_receipt.validate_certification_receipt(
                self.receipt, wrong_image, self.manifest
            )

    def test_receipt_rejects_unknown_duplicate_and_reordered_fields(self) -> None:
        original = self.receipt.read_text(encoding="ascii").splitlines()
        malformed_receipts = {
            "unknown": original + ["unexpected=PASS"],
            "duplicate": original + [original[-1]],
            "reordered": [original[1], original[0], *original[2:]],
        }
        for name, lines in malformed_receipts.items():
            with self.subTest(name=name):
                self.receipt.write_text(
                    "\n".join(lines) + "\n", encoding="ascii", newline=""
                )
                self.assert_rejected()

    def test_receipt_rejects_noncanonical_encoding_and_values(self) -> None:
        cases = {
            "crlf": lambda data: data.replace(b"\n", b"\r\n"),
            "non_ascii": lambda data: data.replace(
                b"tester_label=integration-lab-a",
                "tester_label=láb".encode("utf-8"),
            ),
            "uppercase_hash": lambda data: data.replace(
                self.receipt_values["compressed_image_sha256"].encode("ascii"),
                self.receipt_values["compressed_image_sha256"].upper().encode(
                    "ascii"
                ),
            ),
            "leading_zero_size": lambda data: data.replace(
                f"compressed_image_size={self.image.stat().st_size}".encode(
                    "ascii"
                ),
                f"compressed_image_size=0{self.image.stat().st_size}".encode(
                    "ascii"
                ),
            ),
            "private_tester_identity": lambda data: data.replace(
                b"tester_label=integration-lab-a",
                b"tester_label=serial-abcdef123456",
            ),
            "private_tester_mac": lambda data: data.replace(
                b"tester_label=integration-lab-a",
                b"tester_label=aa-bb-cc-dd-ee-ff",
            ),
        }
        original = self.receipt.read_bytes()
        for name, transform in cases.items():
            with self.subTest(name=name):
                self.receipt.write_bytes(transform(original))
                self.assert_rejected()

    def test_manifest_schema_is_strict_and_ordered(self) -> None:
        lines = self.manifest.read_text(encoding="ascii").splitlines()
        lines[0], lines[1] = lines[1], lines[0]
        self.manifest.write_text(
            "\n".join(lines) + "\n", encoding="ascii", newline=""
        )
        self.refresh_manifest_hash()

        self.assert_rejected("manifest line 1")

    def test_cli_reports_pass_and_fails_closed_without_traceback(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            success = validate_receipt.main(
                [str(self.receipt), str(self.image), str(self.manifest)]
            )
        self.assertEqual(success, 0)
        self.assertIn("certification receipt: PASS", output.getvalue())
        self.assertEqual(errors.getvalue(), "")

        self.receipt_values["hardware_matrix"] = "FAIL"
        self.write_receipt()
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            failure = validate_receipt.main(
                [str(self.receipt), str(self.image), str(self.manifest)]
            )
        self.assertEqual(failure, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("certification receipt: FAIL", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
