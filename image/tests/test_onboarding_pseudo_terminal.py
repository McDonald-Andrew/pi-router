#!/usr/bin/python3
"""Linux pseudo-terminal tests for the browser onboarding runner."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
from pathlib import Path
import sys
import textwrap
import time
import unittest


WEB_DIR = Path(__file__).resolve().parents[2] / "src/web"
sys.path.insert(0, str(WEB_DIR))

from onboarding_pseudo_terminal import (  # noqa: E402
    SETUP_ARGV,
    SETUP_ENV,
    PseudoTerminalStatus,
    PseudoTerminalTimeouts,
    run_post_reboot,
)


POSIX_PSEUDO_TERMINAL = (
    os.name == "posix" and hasattr(os, "fork") and hasattr(os, "openpty")
)

SUCCESS_OUTPUT = """\
setup: Proton managed connection verified
setup: proton0 WireGuard interface verified
setup: bootstrap firewall verified closed
setup: post-reboot stage complete
setup: FINAL REBOOT REQUIRED for unattended verification
"""


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def timeouts(
    *,
    total: float = 2.0,
    username: float = 0.6,
    password: float = 0.6,
    terminate: float = 0.12,
    kill: float = 0.12,
) -> PseudoTerminalTimeouts:
    return PseudoTerminalTimeouts(
        total=total,
        username_prompt=username,
        password_prompt=password,
        write=0.5,
        terminate_grace=terminate,
        kill_grace=kill,
        poll_interval=0.01,
    )


def run_fake(
    source: str,
    username: bytearray,
    password: bytearray,
    *,
    configured_timeouts: PseudoTerminalTimeouts | None = None,
    max_scan_buffer: int = 1024,
):
    return run_post_reboot(
        username,
        password,
        command=(sys.executable, "-u", "-c", textwrap.dedent(source)),
        cwd="/",
        timeouts=configured_timeouts or timeouts(),
        max_scan_buffer=max_scan_buffer,
    )


class OnboardingPseudoTerminalContractTests(unittest.TestCase):
    def test_production_command_and_environment_are_fixed(self) -> None:
        self.assertEqual(
            SETUP_ARGV,
            (
                "/opt/pi-router/current/setup",
                "--live",
                "--allow-live-install",
                "--phase",
                "post-reboot",
            ),
        )
        self.assertEqual(
            SETUP_ENV,
            {
                "PATH": (
                    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:"
                    "/sbin:/bin"
                ),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TERM": "dumb",
                "HOME": "/root",
                "USER": "root",
                "LOGNAME": "root",
            },
        )


@unittest.skipUnless(
    POSIX_PSEUDO_TERMINAL,
    "requires Linux/POSIX fork and pseudo-terminal support",
)
class OnboardingPseudoTerminalTests(unittest.TestCase):
    def test_split_prompts_success_and_credentials_are_zeroed(self) -> None:
        username_value = b"person@example.test"
        password_value = b"p@ss word value"
        username = bytearray(username_value)
        password = bytearray(password_value)
        source = f"""
            import hashlib
            import os
            import sys
            import termios
            import time

            if termios.tcgetattr(0)[3] & (termios.ECHO | termios.ECHONL):
                raise SystemExit(91)

            def emit(parts):
                for part in parts:
                    os.write(1, part.encode("ascii"))
                    time.sleep(0.015)

            emit(["Proton account user", "name or email:"])
            user = sys.stdin.buffer.readline().rstrip(b"\\r\\n")
            if hashlib.sha256(user).hexdigest() != {digest(username_value)!r}:
                raise SystemExit(92)

            emit(["\\nsetup: starting interactive Proton ", "sign-in\\nPass", "word:"])
            secret = sys.stdin.buffer.readline().rstrip(b"\\r\\n")
            if hashlib.sha256(secret).hexdigest() != {digest(password_value)!r}:
                raise SystemExit(93)

            emit([{SUCCESS_OUTPUT[:75]!r}, {SUCCESS_OUTPUT[75:]!r}])
        """

        result = run_fake(source, username, password)

        self.assertEqual(result.status, PseudoTerminalStatus.SUCCESS)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.username_sent)
        self.assertTrue(result.password_sent)
        self.assertTrue(result.all_success_markers)
        self.assertFalse(result.termination_requested)
        self.assertEqual(username, bytearray(len(username_value)))
        self.assertEqual(password, bytearray(len(password_value)))
        self.assertNotIn(username_value.decode(), repr(result))
        self.assertNotIn(password_value.decode(), repr(result))

    def test_authenticated_session_succeeds_without_password_prompt(self) -> None:
        username_value = b"existing@example.test"
        password_value = b"unused-secret"
        username = bytearray(username_value)
        password = bytearray(password_value)
        source = f"""
            import hashlib
            import os
            import sys

            os.write(1, b"Proton account username or email:")
            user = sys.stdin.buffer.readline().rstrip(b"\\r\\n")
            if hashlib.sha256(user).hexdigest() != {digest(username_value)!r}:
                raise SystemExit(80)
            os.write(1, b"\\nsetup: authenticated Proton session detected; sign-in skipped\\n")
            os.write(1, {SUCCESS_OUTPUT.encode()!r})
        """

        result = run_fake(source, username, password)

        self.assertEqual(result.status, PseudoTerminalStatus.SUCCESS)
        self.assertTrue(result.authenticated_session_detected)
        self.assertTrue(result.username_sent)
        self.assertFalse(result.password_prompt_seen)
        self.assertFalse(result.password_sent)
        self.assertEqual(username, bytearray(len(username_value)))
        self.assertEqual(password, bytearray(len(password_value)))

    def test_repeated_password_prompt_is_rejected(self) -> None:
        username = bytearray(b"user")
        password = bytearray(b"secret")
        source = """
            import os
            import sys
            import time

            os.write(1, b"Proton account username or email:")
            sys.stdin.buffer.readline()
            os.write(1, b"\\nsetup: starting interactive Proton sign-in\\nPassword:")
            sys.stdin.buffer.readline()
            os.write(1, b"\\nPassword:")
            time.sleep(10)
        """

        result = run_fake(source, username, password)

        self.assertEqual(
            result.status, PseudoTerminalStatus.REPEATED_PASSWORD_PROMPT
        )
        self.assertTrue(result.password_sent)
        self.assertTrue(result.termination_requested)
        self.assertEqual(username, bytearray(4))
        self.assertEqual(password, bytearray(6))

    def test_two_factor_prompt_is_rejected(self) -> None:
        username = bytearray(b"user")
        password = bytearray(b"secret")
        source = """
            import os
            import sys
            import time

            os.write(1, b"Proton account username or email:")
            sys.stdin.buffer.readline()
            os.write(1, b"\\nsetup: starting interactive Proton sign-in\\nPassword:")
            sys.stdin.buffer.readline()
            os.write(1, b"\\nTwo-factor authentication code:")
            time.sleep(10)
        """

        result = run_fake(source, username, password)

        self.assertEqual(result.status, PseudoTerminalStatus.UNSUPPORTED_AUTH_FLOW)
        self.assertTrue(result.termination_requested)

    def test_sudo_prompt_is_rejected_without_sending_any_credential(self) -> None:
        username = bytearray(b"user")
        password = bytearray(b"secret")
        source = """
            import os
            import time
            os.write(1, b"[sudo] pass")
            os.write(1, b"word for root:")
            time.sleep(10)
        """

        result = run_fake(source, username, password)

        self.assertEqual(result.status, PseudoTerminalStatus.UNEXPECTED_SUDO_PROMPT)
        self.assertFalse(result.username_sent)
        self.assertFalse(result.password_sent)

    def test_username_prompt_timeout_terminates_process_group(self) -> None:
        username = bytearray(b"user")
        password = bytearray(b"secret")
        source = """
            import signal
            import sys
            import time
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(44))
            time.sleep(10)
        """

        result = run_fake(
            source,
            username,
            password,
            configured_timeouts=timeouts(username=0.12),
        )

        self.assertEqual(result.status, PseudoTerminalStatus.USERNAME_PROMPT_TIMEOUT)
        self.assertTrue(result.termination_requested)
        self.assertFalse(result.kill_required)

    def test_password_prompt_timeout_after_signin_marker(self) -> None:
        username = bytearray(b"user")
        password = bytearray(b"secret")
        source = """
            import os
            import sys
            import time
            os.write(1, b"Proton account username or email:")
            sys.stdin.buffer.readline()
            os.write(1, b"\\nsetup: starting interactive Proton sign-in\\n")
            time.sleep(10)
        """

        result = run_fake(
            source,
            username,
            password,
            configured_timeouts=timeouts(password=0.12),
        )

        self.assertEqual(result.status, PseudoTerminalStatus.PASSWORD_PROMPT_TIMEOUT)
        self.assertTrue(result.username_sent)
        self.assertFalse(result.password_sent)

    def test_total_timeout_after_username_without_signin_is_bounded(self) -> None:
        username = bytearray(b"user")
        password = bytearray(b"secret")
        source = """
            import os
            import sys
            import time
            os.write(1, b"Proton account username or email:")
            sys.stdin.buffer.readline()
            time.sleep(10)
        """

        started = time.monotonic()
        result = run_fake(
            source,
            username,
            password,
            configured_timeouts=timeouts(total=0.16, username=0.1),
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result.status, PseudoTerminalStatus.TOTAL_TIMEOUT)
        self.assertTrue(result.termination_requested)
        self.assertLess(elapsed, 1.0)

    def test_sigkill_fallback_is_bounded_for_ignoring_process_group(self) -> None:
        username = bytearray(b"user")
        password = bytearray(b"secret")
        source = """
            import os
            import signal
            import time

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            child = os.fork()
            if child == 0:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                while True:
                    time.sleep(1)
            while True:
                time.sleep(1)
        """

        started = time.monotonic()
        result = run_fake(
            source,
            username,
            password,
            configured_timeouts=timeouts(
                username=0.12, terminate=0.12, kill=0.12
            ),
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result.status, PseudoTerminalStatus.USERNAME_PROMPT_TIMEOUT)
        self.assertTrue(result.termination_requested)
        self.assertTrue(result.kill_required)
        self.assertLess(elapsed, 1.5)

    def test_output_flood_keeps_scanner_bounded_and_retains_split_prompt(self) -> None:
        username_value = b"user"
        password_value = b"secret"
        username = bytearray(username_value)
        password = bytearray(password_value)
        source = f"""
            import hashlib
            import os
            import sys

            payload = b"X" * 131072
            offset = 0
            while offset < len(payload):
                offset += os.write(1, payload[offset:])
            os.write(1, b"Proton account username or email:")
            user = sys.stdin.buffer.readline().rstrip(b"\\r\\n")
            if hashlib.sha256(user).hexdigest() != {digest(username_value)!r}:
                raise SystemExit(71)
            os.write(1, b"\\nsetup: starting interactive Proton sign-in\\nPassword:")
            secret = sys.stdin.buffer.readline().rstrip(b"\\r\\n")
            if hashlib.sha256(secret).hexdigest() != {digest(password_value)!r}:
                raise SystemExit(72)
            os.write(1, {SUCCESS_OUTPUT.encode()!r})
        """

        result = run_fake(
            source,
            username,
            password,
            max_scan_buffer=256,
            configured_timeouts=timeouts(total=4, username=2, password=1),
        )

        self.assertEqual(result.status, PseudoTerminalStatus.SUCCESS)
        self.assertLessEqual(result.scan_buffer_peak, 256)

    def test_child_receives_only_fixed_environment_and_no_credential_argv(self) -> None:
        username_value = b"argv-user-canary"
        password_value = b"argv-password-canary"
        username = bytearray(username_value)
        password = bytearray(password_value)
        source = f"""
            import hashlib
            import os
            import sys

            forbidden = {{
                {len(username_value)}: {digest(username_value)!r},
                {len(password_value)}: {digest(password_value)!r},
            }}
            exposed = [arg.encode() for arg in sys.argv]
            exposed.extend(
                key.encode() + b"=" + value.encode()
                for key, value in os.environ.items()
            )
            for value in exposed:
                for width, expected_hash in forbidden.items():
                    for offset in range(max(0, len(value) - width + 1)):
                        candidate = value[offset:offset + width]
                        if hashlib.sha256(candidate).hexdigest() == expected_hash:
                            raise SystemExit(61)
            if set(os.environ) != {set(SETUP_ENV)!r}:
                raise SystemExit(62)

            os.write(1, b"Proton account username or email:")
            sys.stdin.buffer.readline()
            os.write(1, b"\\nsetup: authenticated Proton session detected; sign-in skipped\\n")
            os.write(1, {SUCCESS_OUTPUT.encode()!r})
        """

        result = run_fake(source, username, password)

        self.assertEqual(result.status, PseudoTerminalStatus.SUCCESS)
        self.assertEqual(result.exit_code, 0)

    def test_raw_pseudo_terminal_output_is_not_returned_or_logged(self) -> None:
        username_value = b"raw-output-user-canary"
        password_value = b"raw-output-password-canary"
        username = bytearray(username_value)
        password = bytearray(password_value)
        source = f"""
            import os
            import sys

            os.write(1, {username_value!r} + b":" + {password_value!r} + b"\\n")
            os.write(1, b"Proton account username or email:")
            sys.stdin.buffer.readline()
            os.write(1, b"\\nsetup: authenticated Proton session detected; sign-in skipped\\n")
            os.write(1, {SUCCESS_OUTPUT.encode()!r})
        """
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = run_fake(source, username, password)

        self.assertEqual(result.status, PseudoTerminalStatus.SUCCESS)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(username, bytearray(len(username_value)))
        self.assertEqual(password, bytearray(len(password_value)))
        self.assertFalse(hasattr(result, "output"))
        captured = stdout.getvalue() + stderr.getvalue() + repr(result)
        for canary in (username_value, password_value):
            self.assertNotIn(canary.decode("ascii"), captured)

    def test_zero_exit_without_required_markers_is_not_success(self) -> None:
        username = bytearray(b"user")
        password = bytearray(b"secret")
        source = """
            import os
            import sys
            os.write(1, b"Proton account username or email:")
            sys.stdin.buffer.readline()
        """

        result = run_fake(source, username, password)

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.status, PseudoTerminalStatus.MISSING_SUCCESS_MARKER)

    def test_nonzero_exit_is_command_failure(self) -> None:
        username = bytearray(b"user")
        password = bytearray(b"secret")
        source = """
            import os
            import sys
            os.write(1, b"Proton account username or email:")
            sys.stdin.buffer.readline()
            raise SystemExit(37)
        """

        result = run_fake(source, username, password)

        self.assertEqual(result.exit_code, 37)
        self.assertEqual(result.status, PseudoTerminalStatus.COMMAND_FAILED)

    def test_control_credentials_are_rejected_and_zeroed_before_spawn(self) -> None:
        for username_value, password_value in (
            (b"bad\nname", b"secret"),
            (b"name", b"bad\x03secret"),
        ):
            with self.subTest(username=username_value, password_length=len(password_value)):
                username = bytearray(username_value)
                password = bytearray(password_value)

                result = run_fake("raise SystemExit(99)", username, password)

                self.assertEqual(
                    result.status, PseudoTerminalStatus.INVALID_CREDENTIAL
                )
                self.assertIsNone(result.exit_code)
                self.assertEqual(username, bytearray(len(username_value)))
                self.assertEqual(password, bytearray(len(password_value)))


if __name__ == "__main__":
    unittest.main()
