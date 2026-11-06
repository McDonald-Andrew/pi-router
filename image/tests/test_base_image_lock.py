from __future__ import annotations

from pathlib import Path
import unittest
from urllib.parse import urlparse


IMAGE_DIR = Path(__file__).resolve().parents[1]
LOCK_FILE = IMAGE_DIR / "base-image.env"


class BaseImageLockTests(unittest.TestCase):
    @staticmethod
    def values() -> dict[str, str]:
        values: dict[str, str] = {}
        for line in LOCK_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or not key or key in values:
                raise AssertionError(f"invalid base-image.env line: {line!r}")
            values[key] = value
        return values

    def test_lock_has_the_complete_immutable_identity(self) -> None:
        values = self.values()

        self.assertEqual(
            set(values),
            {
                "BASE_IMAGE_RELEASE_DATE",
                "BASE_IMAGE_FILENAME",
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
        self.assertEqual(Path(parsed.path).name, values["BASE_IMAGE_FILENAME"])

        for key in (
            "BASE_IMAGE_COMPRESSED_SHA256",
            "BASE_IMAGE_EXTRACTED_SHA256",
        ):
            self.assertRegex(values[key], r"^[0-9a-f]{64}$")
        for key in ("BASE_IMAGE_COMPRESSED_SIZE", "BASE_IMAGE_EXTRACTED_SIZE"):
            self.assertRegex(values[key], r"^[1-9][0-9]*$")
            self.assertGreater(int(values[key]), 0)

    def test_lock_matches_the_reviewed_official_raspberry_pi_os_release(self) -> None:
        values = self.values()

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
            for line in LOCK_FILE.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        )

        self.assertNotRegex(assignments, r"[`;$()]|\\\n")
        for value in self.values().values():
            self.assertNotRegex(value, r"\s")


if __name__ == "__main__":
    unittest.main()
