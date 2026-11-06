#!/usr/bin/env python3

"""Validate a router release tarball before privileged extraction."""

from __future__ import annotations

import pathlib
import sys
import tarfile
from typing import NoReturn


def fail(message: str) -> NoReturn:
    raise SystemExit(f"validate-release-archive: ERROR: {message}")


def normalized_parts(value: str, *, description: str) -> tuple[str, ...]:
    path = pathlib.PurePosixPath(value)
    if path.is_absolute():
        fail(f"{description} is absolute: {value!r}")

    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts or ".." in parts:
        fail(f"{description} is unsafe: {value!r}")
    return parts


def main() -> int:
    if len(sys.argv) != 3:
        fail("usage: validate-release-archive.py ARCHIVE EXPECTED_ROOT")

    archive = pathlib.Path(sys.argv[1])
    expected_root = sys.argv[2]
    expected_parts = normalized_parts(expected_root, description="expected root")
    if len(expected_parts) != 1:
        fail("expected root must be one directory name")

    setup_seen = False
    member_count = 0
    seen_paths: set[tuple[str, ...]] = set()

    try:
        with tarfile.open(archive, mode="r:gz") as release:
            for member in release:
                member_count += 1
                parts = normalized_parts(member.name, description="archive member")
                if parts[0] != expected_root:
                    fail(f"member is outside {expected_root!r}: {member.name!r}")
                if parts in seen_paths:
                    fail(f"duplicate archive member path: {member.name!r}")
                seen_paths.add(parts)

                if member.ischr() or member.isblk() or member.isfifo():
                    fail(f"special filesystem member is not permitted: {member.name!r}")
                if not (
                    member.isfile()
                    or member.isdir()
                    or member.issym()
                    or member.islnk()
                ):
                    fail(f"unsupported archive member type: {member.name!r}")
                if member.mode & 0o6000:
                    fail(f"setuid or setgid mode is not permitted: {member.name!r}")

                if member.issym():
                    target = pathlib.PurePosixPath(member.linkname)
                    if target.is_absolute():
                        fail(f"absolute symlink target is not permitted: {member.name!r}")
                    combined = pathlib.PurePosixPath(*parts[:-1], target)
                    target_parts = normalized_parts(
                        str(combined), description="symlink target"
                    )
                    if target_parts[0] != expected_root:
                        fail(f"symlink escapes release root: {member.name!r}")

                if member.islnk():
                    target_parts = normalized_parts(
                        member.linkname, description="hardlink target"
                    )
                    if target_parts[0] != expected_root:
                        fail(f"hardlink escapes release root: {member.name!r}")

                if parts == (expected_root, "setup"):
                    if not member.isfile():
                        fail("release setup member is not a regular file")
                    setup_seen = True
    except (OSError, tarfile.TarError) as error:
        fail(f"cannot inspect archive: {error}")

    if member_count == 0:
        fail("archive is empty")
    if not setup_seen:
        fail(f"archive does not contain regular file {expected_root}/setup")

    print(
        f"validate-release-archive: PASS: {member_count} members under "
        f"{expected_root}/"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
