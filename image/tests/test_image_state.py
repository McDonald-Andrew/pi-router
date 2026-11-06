from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


FIRSTBOOT_DIR = Path(__file__).resolve().parents[1] / "firstboot"
sys.path.insert(0, str(FIRSTBOOT_DIR))

import pi_router_image_state as image_state  # noqa: E402


BOOT_A = "01234567-89ab-4def-8123-456789abcdef"
BOOT_B = "11234567-89ab-4def-8123-456789abcdef"
BOOT_C = "21234567-89ab-4def-8123-456789abcdef"
RELEASE = "pi-router-v1.2.3-rc.1"
SETUP_SHA256 = "ab" * 32


def pre_full_attempt() -> image_state.Attempt:
    return image_state.Attempt(
        operation="pre-full",
        invocation_boot_id=BOOT_A,
        checkpoint_before="absent",
        release_name=RELEASE,
        release_setup_sha256=SETUP_SHA256,
        late_apply_before="absent",
        original_pre_boot_id="absent",
    )


class RecordValidationTests(unittest.TestCase):
    def test_state_is_exactly_one_canonical_line(self) -> None:
        for state in image_state.VALID_STATES:
            self.assertEqual(image_state.parse_state(f"{state}\n"), state)
            self.assertEqual(image_state.format_state(state), f"{state}\n")

        for invalid in ("", "new", "NEW\n", "new\r\n", "new\nready\n"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(image_state.StateFormatError):
                    image_state.parse_state(invalid)

    def test_attempt_round_trips_in_fixed_field_order(self) -> None:
        attempt = pre_full_attempt()
        text = image_state.format_attempt(attempt)

        self.assertEqual(
            text.splitlines(),
            [
                "format=1",
                "operation=pre-full",
                f"invocation_boot_id={BOOT_A}",
                "checkpoint_before=absent",
                f"release_name={RELEASE}",
                f"release_setup_sha256={SETUP_SHA256}",
                "late_apply_before=absent",
                "original_pre_boot_id=absent",
            ],
        )
        self.assertEqual(image_state.parse_attempt(text), attempt)

    def test_all_attempt_operations_have_strict_semantics(self) -> None:
        attempts = [
            image_state.Attempt(
                operation="config",
                invocation_boot_id=BOOT_A,
                checkpoint_before="absent",
                release_name=RELEASE,
                release_setup_sha256=SETUP_SHA256,
                late_apply_before="absent",
                original_pre_boot_id="absent",
            ),
            pre_full_attempt(),
            image_state.Attempt(
                operation="pre-resume",
                invocation_boot_id=BOOT_B,
                checkpoint_before="absent",
                release_name=RELEASE,
                release_setup_sha256=SETUP_SHA256,
                late_apply_before="present",
                original_pre_boot_id=BOOT_A,
            ),
            image_state.Attempt(
                operation="post",
                invocation_boot_id=BOOT_B,
                checkpoint_before=BOOT_A,
                release_name=RELEASE,
                release_setup_sha256=SETUP_SHA256,
                late_apply_before="not-applicable",
                original_pre_boot_id="not-applicable",
            ),
            image_state.Attempt(
                operation="final",
                invocation_boot_id=BOOT_C,
                checkpoint_before=BOOT_B,
                release_name=RELEASE,
                release_setup_sha256=SETUP_SHA256,
                late_apply_before="not-applicable",
                original_pre_boot_id="not-applicable",
            ),
        ]

        for attempt in attempts:
            with self.subTest(operation=attempt.operation):
                self.assertEqual(
                    image_state.parse_attempt(image_state.format_attempt(attempt)),
                    attempt,
                )

    def test_attempt_rejects_invalid_operation_combinations(self) -> None:
        base = dict(
            invocation_boot_id=BOOT_A,
            release_name=RELEASE,
            release_setup_sha256=SETUP_SHA256,
        )
        invalid = [
            dict(
                operation="config",
                checkpoint_before=BOOT_B,
                late_apply_before="absent",
                original_pre_boot_id="absent",
            ),
            dict(
                operation="pre-full",
                checkpoint_before=BOOT_B,
                late_apply_before="absent",
                original_pre_boot_id="absent",
            ),
            dict(
                operation="pre-resume",
                checkpoint_before="absent",
                late_apply_before="absent",
                original_pre_boot_id=BOOT_B,
            ),
            dict(
                operation="post",
                checkpoint_before="absent",
                late_apply_before="not-applicable",
                original_pre_boot_id="not-applicable",
            ),
            dict(
                operation="final",
                checkpoint_before=BOOT_B,
                late_apply_before="absent",
                original_pre_boot_id="not-applicable",
            ),
        ]
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(image_state.StateFormatError):
                    image_state.Attempt(**base, **values)

    def test_attempt_parser_rejects_schema_deviations(self) -> None:
        canonical = image_state.format_attempt(pre_full_attempt())
        lines = canonical.splitlines()
        reordered = lines.copy()
        reordered[1], reordered[2] = reordered[2], reordered[1]
        duplicate = lines.copy()
        duplicate[2] = duplicate[1]
        deviations = [
            "\n".join(reordered) + "\n",
            "\n".join(duplicate) + "\n",
            canonical.replace("format=1", "format=2"),
            canonical.replace("operation=pre-full", "stage=pre-full"),
            canonical + "extra=value\n",
            canonical[:-1],
            canonical.replace("\n", "\r\n"),
        ]
        for text in deviations:
            with self.subTest(text=text):
                with self.assertRaises(image_state.StateFormatError):
                    image_state.parse_attempt(text)

    def test_uuid_hash_and_release_validators_are_canonical(self) -> None:
        self.assertEqual(image_state.validate_boot_id(BOOT_A), BOOT_A)
        self.assertEqual(image_state.validate_sha256(SETUP_SHA256), SETUP_SHA256)
        self.assertEqual(image_state.validate_release_name(RELEASE), RELEASE)

        invalid_boot_ids = (
            BOOT_A.upper(),
            "00000000-0000-0000-0000-000000000000",
            BOOT_A.replace("-", ""),
            "$(touch /tmp/not-executed)",
        )
        for value in invalid_boot_ids:
            with self.subTest(value=value):
                with self.assertRaises(image_state.StateFormatError):
                    image_state.validate_boot_id(value)

        with self.assertRaises(image_state.StateFormatError):
            image_state.validate_sha256(SETUP_SHA256.upper())
        with self.assertRaises(image_state.StateFormatError):
            image_state.validate_release_name("../pi-router-v1.2.3")
        with self.assertRaises(image_state.StateFormatError):
            image_state.validate_release_name("pi-router-v1.2")

    def test_status_error_and_ready_receipts_round_trip(self) -> None:
        status = image_state.Status("pre-reboot-complete", "waiting-onboarding")
        error = image_state.ErrorReceipt("new", "hardware-mismatch")
        ready = image_state.ReadyReceipt(
            RELEASE, BOOT_C, "checkpoint-consumed-after-durable-intent"
        )

        self.assertEqual(
            image_state.parse_status(image_state.format_status(status)), status
        )
        self.assertEqual(image_state.parse_error(image_state.format_error(error)), error)
        self.assertEqual(image_state.parse_ready(image_state.format_ready(ready)), ready)

    def test_final_service_wait_round_trips_with_canonical_counter(self) -> None:
        wait = image_state.FinalServiceWait(BOOT_C, 7)
        text = image_state.format_final_wait(wait)

        self.assertEqual(
            text,
            f"format=1\nboot_id={BOOT_C}\nchecks_started=7\n",
        )
        self.assertEqual(image_state.parse_final_wait(text), wait)

        for invalid in ("0", "01", "+1", "1000000", "not-a-count"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(image_state.StateFormatError):
                    image_state.parse_final_wait(
                        f"format=1\nboot_id={BOOT_C}\nchecks_started={invalid}\n"
                    )

    def test_receipts_reject_unbounded_or_unknown_values(self) -> None:
        with self.assertRaises(image_state.StateFormatError):
            image_state.Status("new", "ready; touch /tmp/not-executed")
        with self.assertRaises(image_state.StateFormatError):
            image_state.ErrorReceipt("new", "password=secret")
        with self.assertRaises(image_state.StateFormatError):
            image_state.ReadyReceipt(RELEASE, BOOT_A, "trust-me")

    def test_record_parser_rejects_non_ascii_and_oversized_input(self) -> None:
        with self.assertRaises(image_state.StateFormatError):
            image_state.parse_status("format=1\nstate=new\ncode=r\N{LATIN SMALL LETTER E WITH ACUTE}ady\n")
        with self.assertRaises(image_state.StateFormatError):
            image_state.parse_status("x" * (image_state.MAX_RECORD_BYTES + 1))


@unittest.skipUnless(os.name == "posix", "durable state storage targets Pi OS")
class ImageStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "pi-router-image"
        self.store = image_state.ImageStateStore(
            self.root,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )

    def write_raw(self, name: str, payload: bytes, mode: int) -> Path:
        self.store.ensure_directory()
        path = self.root / name
        path.write_bytes(payload)
        path.chmod(mode)
        return path

    def test_absent_state_is_new_and_directory_is_private(self) -> None:
        self.assertEqual(self.store.read_state(), "new")
        self.assertTrue(self.root.is_dir())
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_state_writes_replace_atomically_and_leave_no_temp_files(self) -> None:
        self.assertEqual(self.store.write_state("new"), "new")
        self.assertEqual(self.store.write_state("pre-reboot-complete"), "pre-reboot-complete")
        self.assertEqual(self.store.read_state(), "pre-reboot-complete")
        self.assertEqual((self.root / "state").read_text(encoding="ascii"), "pre-reboot-complete\n")
        self.assertEqual(stat.S_IMODE((self.root / "state").stat().st_mode), 0o600)
        self.assertEqual(list(self.root.glob(".state.tmp-*")), [])

    def test_atomic_write_fsyncs_file_then_directory(self) -> None:
        self.store.ensure_directory()
        real_fsync = os.fsync
        with mock.patch.object(image_state.os, "fsync", wraps=real_fsync) as fsync:
            self.store.write_state("new")

        self.assertEqual(fsync.call_count, 2)

    def test_atomic_write_file_fsync_failure_preserves_previous_record(self) -> None:
        self.store.write_state("new")

        with mock.patch.object(
            image_state.os,
            "fsync",
            side_effect=OSError("injected temporary-file fsync failure"),
        ):
            with self.assertRaises(image_state.StateDurabilityError):
                self.store.write_state("pre-reboot-complete")

        self.assertEqual(self.store.read_state(), "new")
        self.assertEqual(list(self.root.glob(".state.tmp-*")), [])

    def test_atomic_replace_failure_preserves_previous_record(self) -> None:
        self.store.write_state("new")

        with mock.patch.object(
            image_state.os,
            "replace",
            side_effect=OSError("injected atomic-replace failure"),
        ):
            with self.assertRaises(image_state.StateDurabilityError):
                self.store.write_state("pre-reboot-complete")

        self.assertEqual(self.store.read_state(), "new")
        self.assertEqual(list(self.root.glob(".state.tmp-*")), [])

    def test_directory_fsync_failure_leaves_complete_new_record_for_recovery(
        self,
    ) -> None:
        self.store.write_state("new")
        real_fsync = os.fsync
        calls = 0

        def fail_second_fsync(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected state-directory fsync failure")
            real_fsync(descriptor)

        with mock.patch.object(
            image_state.os,
            "fsync",
            side_effect=fail_second_fsync,
        ):
            with self.assertRaises(image_state.StateDurabilityError):
                self.store.write_state("pre-reboot-complete")

        self.assertEqual(calls, 2)
        self.assertEqual(self.store.read_state(), "pre-reboot-complete")
        self.assertEqual(list(self.root.glob(".state.tmp-*")), [])

    def test_attempt_store_round_trip_and_durable_clear(self) -> None:
        attempt = pre_full_attempt()
        self.assertEqual(self.store.write_attempt(attempt), attempt)
        self.assertEqual(self.store.read_attempt(), attempt)
        self.assertEqual(stat.S_IMODE((self.root / "attempt").stat().st_mode), 0o600)

        real_fsync = os.fsync
        with mock.patch.object(image_state.os, "fsync", wraps=real_fsync) as fsync:
            self.assertTrue(self.store.clear_attempt())
        self.assertEqual(fsync.call_count, 1)
        self.assertIsNone(self.store.read_attempt())
        self.assertFalse(self.store.clear_attempt())

    def test_remove_failure_before_unlink_preserves_attempt(self) -> None:
        attempt = pre_full_attempt()
        self.store.write_attempt(attempt)

        with mock.patch.object(
            image_state.os,
            "unlink",
            side_effect=OSError("injected unlink failure"),
        ):
            with self.assertRaises(image_state.StateDurabilityError):
                self.store.clear_attempt()

        self.assertEqual(self.store.read_attempt(), attempt)

    def test_remove_directory_fsync_failure_leaves_absence_for_recovery(self) -> None:
        self.store.write_attempt(pre_full_attempt())

        with mock.patch.object(
            image_state.os,
            "fsync",
            side_effect=OSError("injected remove-directory fsync failure"),
        ):
            with self.assertRaises(image_state.StateDurabilityError):
                self.store.clear_attempt()

        self.assertIsNone(self.store.read_attempt())

    def test_final_service_wait_store_round_trip_and_durable_clear(self) -> None:
        wait = image_state.FinalServiceWait(BOOT_C, 3)
        self.assertEqual(self.store.write_final_wait(wait), wait)
        self.assertEqual(self.store.read_final_wait(), wait)
        self.assertEqual(
            stat.S_IMODE((self.root / image_state.FINAL_WAIT_FILENAME).stat().st_mode),
            0o600,
        )
        self.assertTrue(self.store.clear_final_wait())
        self.assertIsNone(self.store.read_final_wait())

    def test_status_error_and_ready_helpers_use_expected_modes(self) -> None:
        status = self.store.write_status("new", "detecting-hardware")
        error = self.store.write_error("new", "hardware-mismatch")
        ready = self.store.write_ready(RELEASE, BOOT_C, "exit-zero-and-marker")

        self.assertEqual(self.store.read_status(), status)
        self.assertEqual(self.store.read_error(), error)
        self.assertEqual(self.store.read_ready(), ready)
        self.assertEqual(stat.S_IMODE((self.root / "status").stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((self.root / "error").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "ready").stat().st_mode), 0o600)

        self.assertTrue(self.store.clear_status())
        self.assertTrue(self.store.clear_error())
        self.assertTrue(self.store.clear_ready())

    def test_store_rejects_malformed_file_instead_of_sourcing_it(self) -> None:
        sentinel = Path(self.temporary.name) / "sentinel"
        malicious = f"new\n$(touch {sentinel})\n".encode("ascii")
        self.write_raw("state", malicious, 0o600)

        with self.assertRaises(image_state.StateFormatError):
            self.store.read_state()
        self.assertFalse(sentinel.exists())

    def test_store_rejects_wrong_file_mode_for_read_and_replace(self) -> None:
        self.write_raw("state", b"new\n", 0o644)
        with self.assertRaises(image_state.StateSafetyError):
            self.store.read_state()
        with self.assertRaises(image_state.StateSafetyError):
            self.store.write_state("new")

    def test_store_rejects_hard_linked_record(self) -> None:
        state = self.write_raw("state", b"new\n", 0o600)
        os.link(state, self.root / "second-link")
        with self.assertRaises(image_state.StateSafetyError):
            self.store.read_state()

    def test_store_rejects_symlink_without_touching_target(self) -> None:
        self.store.ensure_directory()
        target = Path(self.temporary.name) / "outside"
        target.write_text("outside\n", encoding="ascii")
        target.chmod(0o600)
        try:
            (self.root / "state").symlink_to(target)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        with self.assertRaises(image_state.StateSafetyError):
            self.store.read_state()
        with self.assertRaises(image_state.StateSafetyError):
            self.store.write_state("new")
        self.assertEqual(target.read_text(encoding="ascii"), "outside\n")

    def test_store_rejects_unsafe_directory_permissions(self) -> None:
        self.root.mkdir(mode=0o700)
        self.root.chmod(0o755)
        with self.assertRaises(image_state.StateSafetyError):
            self.store.read_state()

    def test_store_rejects_oversized_file(self) -> None:
        self.write_raw(
            "status", b"x" * (image_state.MAX_RECORD_BYTES + 1), 0o644
        )
        with self.assertRaises(image_state.StateFormatError):
            self.store.read_status()

    def test_durable_remove_rejects_unknown_filename(self) -> None:
        with self.assertRaises(image_state.StateSafetyError):
            self.store.durable_remove("../outside")

    def test_relative_root_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            image_state.ImageStateStore("relative/path")


if __name__ == "__main__":
    unittest.main()
