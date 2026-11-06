from __future__ import annotations

from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BUILD_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "build-image.yml"
RELEASE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"


class GitHubImageWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = BUILD_WORKFLOW.read_text(encoding="utf-8")
        cls.release = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    def test_release_is_the_only_manual_entrypoint(self) -> None:
        self.assertIn("workflow_dispatch:", self.release)
        self.assertIn("workflow_call:", self.build)
        self.assertNotIn("workflow_dispatch:", self.build)
        self.assertNotIn("pull_request:", self.build)
        self.assertNotIn("\n  push:", self.build)
        self.assertIn("uses: ./.github/workflows/build-image.yml", self.release)
        self.assertIn("profile: release", self.release)

    def test_workflow_calls_the_single_local_builder(self) -> None:
        self.assertIn(
            'bash image/build-local "$VERSION" "$OUTPUT_DIR" "$WORK_DIR" "$PROFILE"',
            self.build,
        )
        self.assertIn("actions/cache@v4", self.build)
        self.assertIn("image/base-image.env", self.build)
        self.assertIn("sudo losetup --find", self.build)
        self.assertIn("minimum_bytes=$((11 * 1024 * 1024 * 1024))", self.build)

    def test_manifest_targets_the_exact_release_asset(self) -> None:
        self.assertIn("PI_ROUTER_IMAGER_IMAGE_URL", self.build)
        self.assertIn("https://github.com/${GITHUB_REPOSITORY}/releases/download/", self.build)
        self.assertIn("EXPECTED_IMAGE_URL", self.build)
        self.assertIn('entry["url"] != expected_url', self.build)

    def test_four_image_files_go_directly_to_the_release(self) -> None:
        for suffix in (
            ".img.xz",
            ".img.xz.sha256",
            ".manifest.txt",
            ".rpi-imager-manifest",
        ):
            with self.subTest(suffix=suffix):
                self.assertIn(f"$OUTPUT_DIR/$IMAGE_NAME{suffix}", self.build)
        self.assertIn("verify-build-outputs: PASS: 4 files", self.build)
        self.assertIn('gh release upload "$RELEASE_TAG"', self.build)
        self.assertNotIn("actions/upload-artifact", self.build)
        self.assertNotIn("--clobber", self.build)

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
