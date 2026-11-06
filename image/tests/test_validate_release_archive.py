from __future__ import annotations

import io
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import unittest


VALIDATOR = pathlib.Path(__file__).resolve().parents[1] / "validate-release-archive.py"
RELEASE_ROOT = "pi-router-v1.2.3"


class ReleaseArchiveValidationTests(unittest.TestCase):
    def write_archive(self, members: list[tarfile.TarInfo]) -> pathlib.Path:
        temporary = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
        temporary.close()
        archive = pathlib.Path(temporary.name)
        self.addCleanup(archive.unlink, missing_ok=True)

        with tarfile.open(archive, mode="w:gz") as output:
            for member in members:
                payload = b"#!/usr/bin/env bash\n" if member.isfile() else b""
                member.size = len(payload)
                output.addfile(member, io.BytesIO(payload) if payload else None)

        return archive

    def validate(self, archive: pathlib.Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(VALIDATOR), str(archive), RELEASE_ROOT],
            check=False,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def setup_member() -> tarfile.TarInfo:
        setup = tarfile.TarInfo(f"{RELEASE_ROOT}/setup")
        setup.mode = 0o755
        return setup

    def test_accepts_one_root_with_executable_setup(self) -> None:
        root = tarfile.TarInfo(RELEASE_ROOT)
        root.type = tarfile.DIRTYPE
        archive = self.write_archive([root, self.setup_member()])

        result = self.validate(archive)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_rejects_member_outside_expected_root(self) -> None:
        foreign = tarfile.TarInfo("other/file")
        archive = self.write_archive([self.setup_member(), foreign])

        result = self.validate(archive)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside", result.stderr)

    def test_rejects_parent_path_component(self) -> None:
        traversal = tarfile.TarInfo(f"{RELEASE_ROOT}/../outside")
        archive = self.write_archive([self.setup_member(), traversal])

        result = self.validate(archive)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe", result.stderr)

    def test_rejects_escaping_symlink(self) -> None:
        symlink = tarfile.TarInfo(f"{RELEASE_ROOT}/link")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "../../outside"
        archive = self.write_archive([self.setup_member(), symlink])

        result = self.validate(archive)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe", result.stderr)

    def test_accepts_non_executable_setup_from_current_release_workflow(self) -> None:
        setup = self.setup_member()
        setup.mode = 0o644
        archive = self.write_archive([setup])

        result = self.validate(archive)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_duplicate_member_path(self) -> None:
        first = self.setup_member()
        second = self.setup_member()
        archive = self.write_archive([first, second])

        result = self.validate(archive)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate", result.stderr)

    def test_rejects_setuid_member(self) -> None:
        setup = self.setup_member()
        setup.mode = 0o4755
        archive = self.write_archive([setup])

        result = self.validate(archive)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("setuid", result.stderr)


if __name__ == "__main__":
    unittest.main()
