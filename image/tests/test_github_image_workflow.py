from __future__ import annotations

from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"


class GitHubImageWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.release = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    def test_release_is_the_single_image_workflow(self) -> None:
        self.assertIn("workflow_dispatch:", self.release)
        self.assertNotIn("workflow_call:", self.release)
        self.assertFalse(
            (REPOSITORY_ROOT / ".github" / "workflows" / "build-image.yml").exists()
        )

    def test_release_calls_the_single_local_builder(self) -> None:
        self.assertIn(
            'bash image/build-local "$VERSION" "$OUTPUT_DIR" "$WORK_DIR" release',
            self.release,
        )
        self.assertIn("actions/cache@v4", self.release)
        self.assertIn("image/base-image.env", self.release)
        self.assertIn("sudo losetup --find", self.release)
        self.assertIn(
            "minimum_bytes=$((11 * 1024 * 1024 * 1024))", self.release
        )

    def test_job_level_work_directory_uses_an_available_context(self) -> None:
        work_dir = "${{ github.workspace }}/.pi-router-image-work"

        self.assertIn(f"WORK_DIR: {work_dir}", self.release)
        self.assertIn(f"path: {work_dir}/base-image-cache", self.release)
        self.assertNotRegex(self.release, r"(?m)^\s+WORK_DIR:.*runner\.")

    def test_manifest_targets_the_exact_release_asset(self) -> None:
        self.assertIn("PI_ROUTER_IMAGER_IMAGE_URL", self.release)
        self.assertIn(
            "https://github.com/${GITHUB_REPOSITORY}/releases/download/",
            self.release,
        )

    def test_four_image_files_go_directly_to_the_release(self) -> None:
        for suffix in (
            ".img.xz",
            ".img.xz.sha256",
            ".manifest.txt",
            ".rpi-imager-manifest",
        ):
            with self.subTest(suffix=suffix):
                self.assertIn(f"$OUTPUT_DIR/$image_name{suffix}", self.release)
        self.assertIn('gh release upload "$RELEASE_TAG"', self.release)
        self.assertNotIn("actions/upload-artifact", self.release)
        self.assertNotIn("--clobber", self.release)

    def test_release_stays_a_prerelease_until_hardware_certification(self) -> None:
        self.assertIn("--draft", self.release)
        self.assertIn("--prerelease", self.release)
        self.assertIn("Hardware certification is pending", self.release)
        self.assertIn("gh release edit $env:RELEASE_TAG --draft=false", self.release)
        self.assertIn("-not $published.isPrerelease", self.release)
        self.assertIn("cleanup_failed_draft:", self.release)
        self.assertIn("--cleanup-tag --yes", self.release)

    def test_release_requires_the_complete_nine_asset_set(self) -> None:
        for name in (
            "pi-router-installer.exe",
            "pi-router-installer.sha256.txt",
            "THIRD-PARTY-NOTICES.txt",
            "$imageName.img.xz",
            "$imageName.img.xz.sha256",
            "$imageName.manifest.txt",
            "$imageName.rpi-imager-manifest",
        ):
            with self.subTest(name=name):
                self.assertIn(name, self.release)
        self.assertIn("Compare-Object $expected $actual", self.release)


if __name__ == "__main__":
    unittest.main()
