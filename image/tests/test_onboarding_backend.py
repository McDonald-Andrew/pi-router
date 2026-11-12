from __future__ import annotations

import contextlib
import io
import os
import socket
import stat
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Callable, Sequence


FIRSTBOOT_DIR = Path(__file__).resolve().parents[1] / "firstboot"
sys.path.insert(0, str(FIRSTBOOT_DIR))

import pi_router_onboarding_pseudo_terminal as pseudo_terminal  # noqa: E402
import pi_router_image_state as image_state  # noqa: E402
import pi_router_onboarding_backend as backend_module  # noqa: E402


BOOT_A = "01234567-89ab-4def-8123-456789abcdef"
BOOT_B = "11234567-89ab-4def-8123-456789abcdef"
RELEASE = backend_module.ReleaseInfo("pi-router-v1.2.3", "ab" * 32)


def credential_frame(username: bytes = b"account", password: bytes = b"secret") -> bytearray:
    return bytearray(
        bytes((1,))
        + len(username).to_bytes(2, "big")
        + len(password).to_bytes(2, "big")
        + username
        + password
    )


def pseudo_terminal_result(
    status: pseudo_terminal.PseudoTerminalStatus,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(status=status)


class FakeRunner(backend_module.CommandRunner):
    def __init__(
        self,
        callback: Callable[[tuple[str, ...]], backend_module.CommandResult] | None = None,
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.timeouts: list[int] = []
        self.callback = callback
        self.route_available = True

    def run(
        self, argv: Sequence[str], *, timeout: int = 30
    ) -> backend_module.CommandResult:
        self.timeouts.append(timeout)
        command = tuple(argv)
        self.commands.append(command)
        if command[:5] == (
            backend_module.IP,
            "-4",
            "route",
            "show",
            "default",
        ):
            if self.route_available:
                return backend_module.CommandResult(
                    0, "default via 192.0.2.1 dev wan0\n"
                )
            return backend_module.CommandResult(0, "")
        if self.callback is not None:
            return self.callback(command)
        if command[:6] == (
            backend_module.NFT,
            "-a",
            "list",
            "chain",
            "inet",
            "router_filter",
        ):
            return backend_module.CommandResult(
                0,
                "chain host_wan_bootstrap {\n  counter packets 0 bytes 0\n}\n",
            )
        return backend_module.CommandResult(0, "")


class BackendFixture:
    def __init__(self, test: unittest.TestCase) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        test.addCleanup(self.temporary.cleanup)
        self.paths = backend_module.BackendPaths(Path(self.temporary.name))
        self.paths.state_directory.parent.mkdir(parents=True, exist_ok=True)
        self.store = image_state.ImageStateStore(
            self.paths.state_directory,
            expected_uid=None,
            expected_gid=None,
        )
        self.store.write_state("pre-reboot-complete")
        self.store.write_status("pre-reboot-complete", "waiting-onboarding")
        self.write_checkpoint(BOOT_A)
        self.write_router_config()
        self.set_carrier("lan0", True)
        self.set_carrier("wan0", True)

    def write_checkpoint(self, value: str, *, mode: int = 0o600) -> None:
        path = self.paths.checkpoint
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="ascii")
        path.chmod(mode)

    def remove_checkpoint(self) -> None:
        try:
            self.paths.checkpoint.unlink()
        except FileNotFoundError:
            pass

    def write_router_config(self, text: str | None = None) -> None:
        path = self.paths.router_config
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            text
            if text is not None
            else 'LAN_IF="lan0"\nWAN_IF="wan0"\n',
            encoding="ascii",
        )
        path.chmod(0o644)

    def set_carrier(self, interface: str, connected: bool) -> None:
        path = self.paths.path(f"/sys/class/net/{interface}/carrier")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1\n" if connected else "0\n", encoding="ascii")

    def provisioning(self) -> None:
        path = self.paths.provisioning
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("dhcp-option=3\n", encoding="ascii")
        path.chmod(0o644)

    def backend(
        self,
        pseudo_terminal_runner: Callable[[bytearray, bytearray], object],
        *,
        runner: FakeRunner | None = None,
        launcher: Callable[[Callable[[], None]], None] | None = None,
    ) -> backend_module.OnboardingBackend:
        return backend_module.OnboardingBackend(
            paths=self.paths,
            store=self.store,
            runner=runner or FakeRunner(),
            pseudo_terminal_runner=pseudo_terminal_runner,  # type: ignore[arg-type]
            worker_launcher=launcher or (lambda target: target()),
            lock_factory=contextlib.nullcontext,
            expected_uid=None,
            expected_gid=None,
            boot_id_override=BOOT_B,
            release_override=RELEASE,
            success_status_grace_seconds=0,
        )


class OnboardingBackendTests(unittest.TestCase):
    def test_status_tracks_live_cables_and_wan_route_then_recovers(self) -> None:
        fixture = BackendFixture(self)
        runner = FakeRunner()
        backend = fixture.backend(
            lambda _username, _password: pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.COMMAND_FAILED
            ),
            runner=runner,
        )

        self.assertEqual(backend.status_code(), "waiting-onboarding")
        fixture.set_carrier("lan0", False)
        self.assertEqual(backend.status_code(), "waiting-lan-carrier")
        fixture.set_carrier("lan0", True)
        fixture.set_carrier("wan0", False)
        self.assertEqual(backend.status_code(), "waiting-wan-carrier")
        fixture.set_carrier("wan0", True)
        runner.route_available = False
        self.assertEqual(backend.status_code(), "waiting-wan-dhcp")
        runner.route_available = True
        self.assertEqual(backend.status_code(), "waiting-onboarding")
        self.assertEqual(fixture.store.read_status().code, "waiting-onboarding")

    def test_submission_is_gated_by_each_live_prerequisite(self) -> None:
        fixture = BackendFixture(self)
        runner = FakeRunner()
        calls: list[bool] = []
        backend = fixture.backend(
            lambda _username, _password: (
                calls.append(True),
                pseudo_terminal_result(
                    pseudo_terminal.PseudoTerminalStatus.COMMAND_FAILED
                ),
            )[1],
            runner=runner,
        )

        fixture.set_carrier("lan0", False)
        frame = credential_frame()
        self.assertEqual(backend.submit_frame(frame), backend_module.RESPONSE_NOT_READY)
        self.assertEqual(frame, bytearray(len(frame)))
        fixture.set_carrier("lan0", True)
        fixture.set_carrier("wan0", False)
        self.assertEqual(
            backend.submit_frame(credential_frame()),
            backend_module.RESPONSE_NOT_READY,
        )
        fixture.set_carrier("wan0", True)
        runner.route_available = False
        self.assertEqual(
            backend.submit_frame(credential_frame()),
            backend_module.RESPONSE_NOT_READY,
        )
        self.assertEqual(calls, [])
        self.assertIsNone(fixture.store.read_attempt())
        self.assertIsNone(fixture.store.read_error())

        runner.route_available = True
        self.assertEqual(
            backend.submit_frame(credential_frame()),
            backend_module.RESPONSE_ACCEPTED,
        )
        self.assertEqual(calls, [True])

    def test_live_status_uses_strict_detected_config_and_fixed_route_command(self) -> None:
        fixture = BackendFixture(self)
        runner = FakeRunner()
        backend = fixture.backend(
            lambda _username, _password: pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.COMMAND_FAILED
            ),
            runner=runner,
        )

        self.assertEqual(backend.status_code(), "waiting-onboarding")
        self.assertIn(
            (
                backend_module.IP,
                "-4",
                "route",
                "show",
                "default",
                "dev",
                "wan0",
            ),
            runner.commands,
        )
        self.assertIn(backend_module.LIVE_CHECK_TIMEOUT_SECONDS, runner.timeouts)
        fixture.write_router_config('LAN_IF="lan0"\nLAN_IF="other"\nWAN_IF="wan0"\n')
        self.assertEqual(backend.status_code(), "error")

    def test_active_worker_status_wins_over_disappearing_prerequisites(self) -> None:
        fixture = BackendFixture(self)
        backend = fixture.backend(
            lambda _username, _password: pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.COMMAND_FAILED
            )
        )
        fixture.set_carrier("lan0", False)
        with backend._guard:
            backend._worker_active = True
        self.assertEqual(backend.status_code(), "running-post-reboot")

    def test_attempt_is_durable_before_pseudo_terminal_and_commits(self) -> None:
        fixture = BackendFixture(self)
        fixture.provisioning()
        observed: dict[str, object] = {}

        def run(username: bytearray, password: bytearray) -> object:
            attempt = fixture.store.read_attempt()
            self.assertIsNotNone(attempt)
            assert attempt is not None
            self.assertEqual(attempt.operation, "post")
            self.assertEqual(attempt.invocation_boot_id, BOOT_B)
            self.assertEqual(attempt.checkpoint_before, BOOT_A)
            self.assertEqual(fixture.store.read_status().code, "running-post-reboot")
            observed["username"] = username
            observed["password"] = password
            fixture.write_checkpoint(BOOT_B)
            fixture.paths.provisioning.unlink()
            return pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.SUCCESS
            )

        runner = FakeRunner()
        backend = fixture.backend(run, runner=runner)
        frame = credential_frame()

        self.assertEqual(backend.submit_frame(frame), backend_module.RESPONSE_ACCEPTED)
        self.assertEqual(frame, bytearray(len(frame)))
        self.assertEqual(fixture.store.read_state(), "post-reboot-complete")
        self.assertIsNone(fixture.store.read_attempt())
        self.assertEqual(fixture.store.read_status().code, "rebooting-after-post")
        self.assertEqual(backend.status_code(), "rebooting-after-post")
        self.assertEqual(observed["username"], bytearray(7))
        self.assertEqual(observed["password"], bytearray(6))
        self.assertIn(
            (backend_module.SYSTEMCTL, "--no-block", "reboot"),
            runner.commands,
        )

    def test_checkpoint_commit_reconciles_pseudo_terminal_failure(self) -> None:
        fixture = BackendFixture(self)

        def run(_username: bytearray, _password: bytearray) -> object:
            fixture.write_checkpoint(BOOT_B)
            return pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.COMMAND_FAILED
            )

        backend = fixture.backend(run)
        self.assertEqual(
            backend.submit_frame(credential_frame()),
            backend_module.RESPONSE_ACCEPTED,
        )
        self.assertEqual(fixture.store.read_state(), "post-reboot-complete")
        self.assertIsNone(fixture.store.read_error())

    def test_retry_safe_failure_clears_attempt_and_keeps_pre_state(self) -> None:
        fixture = BackendFixture(self)

        backend = fixture.backend(
            lambda _username, _password: pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.COMMAND_FAILED
            )
        )
        self.assertEqual(
            backend.submit_frame(credential_frame()),
            backend_module.RESPONSE_ACCEPTED,
        )
        self.assertEqual(fixture.store.read_state(), "pre-reboot-complete")
        self.assertIsNone(fixture.store.read_attempt())
        self.assertIsNone(fixture.store.read_error())
        self.assertEqual(fixture.store.read_status().code, "waiting-onboarding")

    def test_open_bootstrap_after_failure_is_terminal(self) -> None:
        fixture = BackendFixture(self)
        runner = FakeRunner(
            lambda command: backend_module.CommandResult(
                0, "chain host_wan_bootstrap { accept }\n"
            )
            if command[0] == backend_module.NFT
            else backend_module.CommandResult(0, "")
        )
        backend = fixture.backend(
            lambda _username, _password: pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.COMMAND_FAILED
            ),
            runner=runner,
        )
        self.assertEqual(
            backend.submit_frame(credential_frame()),
            backend_module.RESPONSE_ACCEPTED,
        )
        self.assertEqual(fixture.store.read_error().code, "setup-post-failed")
        self.assertEqual(fixture.store.read_status().code, "error")

    def test_malformed_checkpoint_fails_closed_without_repair(self) -> None:
        fixture = BackendFixture(self)
        fixture.write_checkpoint("not-a-uuid")
        frame = credential_frame()
        backend = fixture.backend(
            lambda _username, _password: pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.SUCCESS
            )
        )

        self.assertEqual(backend.submit_frame(frame), backend_module.RESPONSE_FAILED)
        self.assertEqual(frame, bytearray(len(frame)))
        self.assertEqual(fixture.store.read_error().code, "checkpoint-corrupt")
        self.assertEqual(
            fixture.paths.checkpoint.read_text(encoding="ascii"),
            "not-a-uuid\n",
        )

    def test_not_ready_busy_and_invalid_are_fixed_responses(self) -> None:
        fixture = BackendFixture(self)
        backend = fixture.backend(
            lambda _username, _password: pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.SUCCESS
            )
        )
        invalid = bytearray(b"bad")
        self.assertEqual(backend.submit_frame(invalid), backend_module.RESPONSE_INVALID)
        self.assertEqual(invalid, bytearray(3))

        terminal_control = credential_frame(password=b"bad\x03secret")
        self.assertEqual(
            backend.submit_frame(terminal_control),
            backend_module.RESPONSE_INVALID,
        )
        self.assertEqual(terminal_control, bytearray(len(terminal_control)))

        with backend._guard:
            backend._worker_active = True
        busy = credential_frame()
        self.assertEqual(backend.submit_frame(busy), backend_module.RESPONSE_BUSY)
        self.assertEqual(busy, bytearray(len(busy)))
        with backend._guard:
            backend._worker_active = False

        fixture.store.write_state("ready")
        not_ready = credential_frame()
        self.assertEqual(
            backend.submit_frame(not_ready), backend_module.RESPONSE_NOT_READY
        )
        self.assertEqual(not_ready, bytearray(len(not_ready)))

    def test_worker_launcher_failure_wipes_credentials(self) -> None:
        fixture = BackendFixture(self)
        captured: dict[str, bytearray] = {}

        def launcher(target: Callable[[], None]) -> None:
            defaults = target.__defaults__
            assert defaults is not None
            captured["username"] = defaults[0]
            captured["password"] = defaults[1]
            raise RuntimeError

        backend = fixture.backend(
            lambda _username, _password: pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.SUCCESS
            ),
            launcher=launcher,
        )
        self.assertEqual(
            backend.submit_frame(credential_frame()), backend_module.RESPONSE_FAILED
        )
        self.assertEqual(captured["username"], bytearray(7))
        self.assertEqual(captured["password"], bytearray(6))

    def test_credential_exception_is_not_logged_or_persisted(self) -> None:
        fixture = BackendFixture(self)
        username = b"redaction-user-canary@example.test"
        password = b"redaction-password-canary"
        frame = credential_frame(username, password)
        runner = FakeRunner()

        def fail_with_secret_message(
            received_username: bytearray, received_password: bytearray
        ) -> object:
            raise RuntimeError(
                "authentication failed for "
                f"{received_username.decode('utf-8')}:"
                f"{received_password.decode('utf-8')}"
            )

        backend = fixture.backend(fail_with_secret_message, runner=runner)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            response = backend.submit_frame(frame)

        self.assertEqual(response, backend_module.RESPONSE_ACCEPTED)
        self.assertEqual(frame, bytearray(len(frame)))
        self.assertEqual(fixture.store.read_state(), "pre-reboot-complete")
        self.assertIsNone(fixture.store.read_attempt())
        self.assertIsNone(fixture.store.read_error())

        persisted = bytearray()
        for path in fixture.paths.state_directory.rglob("*"):
            if path.is_file():
                persisted.extend(path.read_bytes())
        command_text = "\n".join("\0".join(command) for command in runner.commands)
        captured_text = stdout.getvalue() + stderr.getvalue() + command_text
        for canary in (username, password):
            self.assertNotIn(canary, persisted)
            self.assertNotIn(canary.decode("utf-8"), captured_text)


@unittest.skipUnless(os.name == "posix", "SO_PEERCRED is Linux/POSIX-specific")
class BrokerProtocolTests(unittest.TestCase):
    def test_peer_identity_must_match_exact_uid_and_gid(self) -> None:
        fixture = BackendFixture(self)
        backend = fixture.backend(
            lambda _username, _password: pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.SUCCESS
            )
        )
        server = backend_module.BrokerServer(
            backend,
            frontend_uid=os.getuid(),
            frontend_gid=os.getgid(),
            paths=fixture.paths,
        )
        peer = types.SimpleNamespace(
            getsockopt=lambda *_args: struct.pack(
                "3i", os.getpid(), os.getuid(), os.getgid()
            )
        )
        self.assertTrue(server._peer_allowed(peer))  # type: ignore[arg-type]
        wrong = types.SimpleNamespace(
            getsockopt=lambda *_args: struct.pack(
                "3i", os.getpid(), os.getuid() + 1, os.getgid()
            )
        )
        self.assertFalse(server._peer_allowed(wrong))  # type: ignore[arg-type]

    def test_status_protocol_is_one_fixed_seqpacket(self) -> None:
        fixture = BackendFixture(self)
        backend = fixture.backend(
            lambda _username, _password: pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.SUCCESS
            )
        )
        server = backend_module.BrokerServer(
            backend,
            frontend_uid=os.getuid(),
            frontend_gid=os.getgid(),
            paths=fixture.paths,
        )
        client, service = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            client.sendall(backend_module.STATUS_REQUEST)
            server.handle_connection(service)
            self.assertEqual(client.recv(128), b"waiting-onboarding\n")
        finally:
            client.close()
            service.close()

    def test_serve_preserves_listener_opened_before_systemd_ready(self) -> None:
        fixture = BackendFixture(self)
        backend = fixture.backend(
            lambda _username, _password: pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.SUCCESS
            )
        )
        backend.stop_event.set()
        server = backend_module.BrokerServer(
            backend,
            frontend_uid=os.getuid(),
            frontend_gid=os.getgid(),
            paths=fixture.paths,
        )
        closed: list[bool] = []
        server.listener = types.SimpleNamespace(close=lambda: closed.append(True))  # type: ignore[assignment]
        opened: list[bool] = []
        server.open = lambda: opened.append(True)  # type: ignore[method-assign]

        server.serve()

        self.assertEqual(opened, [])
        self.assertEqual(closed, [True])

    def test_open_creates_exact_runtime_directory_despite_private_umask(self) -> None:
        fixture = BackendFixture(self)
        fixture.paths.runtime_directory.parent.mkdir(parents=True, exist_ok=True)
        backend = fixture.backend(
            lambda _username, _password: pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.COMMAND_FAILED
            )
        )
        server = backend_module.BrokerServer(
            backend,
            frontend_uid=os.getuid(),
            frontend_gid=os.getgid(),
            owner_uid=os.getuid(),
            paths=fixture.paths,
        )
        previous_umask = os.umask(0o077)
        try:
            server.open()
        finally:
            os.umask(previous_umask)
        try:
            directory = fixture.paths.runtime_directory.stat()
            endpoint = fixture.paths.socket.stat()
            self.assertEqual(stat.S_IMODE(directory.st_mode), 0o750)
            self.assertEqual(directory.st_uid, os.getuid())
            self.assertEqual(directory.st_gid, os.getgid())
            self.assertTrue(stat.S_ISSOCK(endpoint.st_mode))
            self.assertEqual(stat.S_IMODE(endpoint.st_mode), 0o660)
        finally:
            server.close()

    def test_credential_packet_is_received_mutably_and_wiped(self) -> None:
        fixture = BackendFixture(self)
        backend = fixture.backend(
            lambda _username, _password: pseudo_terminal_result(
                pseudo_terminal.PseudoTerminalStatus.COMMAND_FAILED
            )
        )
        server = backend_module.BrokerServer(
            backend,
            frontend_uid=os.getuid(),
            frontend_gid=os.getgid(),
            paths=fixture.paths,
        )
        payload = credential_frame()

        class FakeConnection:
            def __init__(self) -> None:
                self.received_storage: bytearray | None = None
                self.response = b""

            def settimeout(self, _timeout: float) -> None:
                pass

            def getsockopt(self, *_args: object) -> bytes:
                return struct.pack("3i", os.getpid(), os.getuid(), os.getgid())

            def recvmsg(self, *_args: object) -> object:
                raise AssertionError("immutable recvmsg must not be used")

            def recvmsg_into(
                self, buffers: tuple[memoryview, ...], _ancillary: int
            ) -> tuple[int, list[object], int, None]:
                view = buffers[0]
                self.received_storage = view.obj
                view[: len(payload)] = payload
                return len(payload), [], 0, None

            def send(self, response: bytes) -> int:
                self.response = response
                return len(response)

        connection = FakeConnection()
        server.handle_connection(connection)  # type: ignore[arg-type]

        self.assertEqual(connection.response, backend_module.RESPONSE_ACCEPTED)
        self.assertIsNotNone(connection.received_storage)
        assert connection.received_storage is not None
        self.assertEqual(
            connection.received_storage,
            bytearray(len(connection.received_storage)),
        )


class TLSContractTests(unittest.TestCase):
    def test_runtime_certificate_outlives_delayed_onboarding(self) -> None:
        self.assertGreaterEqual(backend_module.TLS_VALIDITY_DAYS, 3650)

    def test_lan_address_is_strictly_derived_from_router_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = backend_module.BackendPaths(Path(temporary))
            config = paths.router_config
            config.parent.mkdir(parents=True)
            config.write_text(
                '# generated\nLAN_ADDR="192.168.50.1"\n', encoding="ascii"
            )
            config.chmod(0o644)
            material = backend_module.TLSMaterial(
                paths,
                expected_uid=None,
                expected_gid=None,
            )
            self.assertEqual(material._lan_address(), "192.168.50.1")

    def test_backend_unit_notifies_only_after_runtime_material_exists(self) -> None:
        unit = (
            FIRSTBOOT_DIR / "pi-router-onboarding-backend.service"
        ).read_text(encoding="utf-8")
        frontend = (FIRSTBOOT_DIR / "pi-router-onboarding.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("Type=notify", unit)
        self.assertIn("NotifyAccess=main", unit)
        self.assertIn("LimitCORE=0", unit)
        self.assertIn("MemorySwapMax=0", unit)
        self.assertIn("Requires=pi-router-onboarding-backend.service", frontend)
        self.assertIn("After=network.target pi-router-onboarding-backend.service", frontend)
        self.assertIn("LoadCredential=tls.crt:/run/pi-router-image/onboarding.crt", frontend)
        self.assertIn("LoadCredential=tls.key:/run/pi-router-image/onboarding.key", frontend)

    def test_backend_does_not_make_immutable_credential_copies(self) -> None:
        source = (
            FIRSTBOOT_DIR / "pi_router_onboarding_backend.py"
        ).read_text(encoding="utf-8")
        entry = (FIRSTBOOT_DIR / "pi-router-onboarding-backend").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("bytes(username)", source)
        self.assertNotIn("bytes(password)", source)
        self.assertIn("exec /usr/bin/python3 -B", entry)


if __name__ == "__main__":
    unittest.main()
