from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Sequence
from unittest import mock


FIRSTBOOT_DIR = Path(__file__).resolve().parents[1] / "firstboot"
sys.path.insert(0, str(FIRSTBOOT_DIR))

import pi_router_image_controller as controller_module  # noqa: E402
import pi_router_image_state as image_state  # noqa: E402
import pi_router_onboarding_frontend as onboarding_frontend  # noqa: E402


BOOT_A = "01234567-89ab-4def-8123-456789abcdef"
BOOT_B = "11234567-89ab-4def-8123-456789abcdef"
BOOT_C = "21234567-89ab-4def-8123-456789abcdef"
RELEASE = controller_module.ReleaseInfo("pi-router-v1.2.3", "ab" * 32)


def result(returncode: int = 0, output: str = "") -> controller_module.CommandResult:
    return controller_module.CommandResult(returncode, output)


def default_result(command: tuple[str, ...]) -> controller_module.CommandResult:
    joined = " ".join(command)
    if command == ("/usr/bin/getent", "passwd", "admin"):
        return result(
            0,
            "admin:x:1000:1000:Pi Router administrator:/home/admin:/bin/bash\n",
        )
    if command == ("/usr/bin/getent", "group", "admin"):
        return result(0, "admin:x:1000:\n")
    if command == ("/usr/bin/passwd", "--status", "admin"):
        return result(0, "admin P 2026-11-06 0 99999 7 -1\n")
    if command == ("/usr/bin/systemctl", "is-enabled", "ssh.service"):
        return result(1, "disabled\n")
    if "connection show id pi-router-image-wan-bootstrap" in joined:
        return result(10)
    if command[:5] == (
        "/usr/sbin/ip",
        "-4",
        "route",
        "show",
        "default",
    ):
        return result(0, "default via 198.51.100.1 dev wan0 proto dhcp\n")
    if command[:2] == ("/usr/bin/getent", "ahostsv4"):
        return result(0, "198.51.100.20 STREAM repo.protonvpn.com\n")
    if command[:4] == ("/usr/sbin/ip", "-4", "route", "get"):
        return result(
            0,
            "198.51.100.20 via 198.51.100.1 dev wan0 src 198.51.100.2\n",
        )
    if "GENERAL.CONNECTION device show lan0" in joined:
        return result(0, "LAN\n")
    if "GENERAL.CONNECTION device show wan0" in joined:
        return result(0, "WAN\n")
    if "GENERAL.TYPE device show proton0" in joined:
        return result(0, "wireguard\n")
    if command[:6] == (
        "/usr/sbin/ip",
        "-4",
        "-o",
        "addr",
        "show",
        "dev",
    ):
        return result(0, "2: lan0 inet 192.168.50.1/24 scope global lan0\n")
    if command[:5] == (
        "/usr/sbin/nft",
        "list",
        "table",
        "inet",
        "router_filter",
    ):
        return result(
            0,
            "table inet router_filter {\n"
            " chain forward { type filter hook forward priority filter; policy drop; }\n"
            " chain host_wan_bootstrap { }\n"
            "}\n",
        )
    if command == ("/usr/sbin/sysctl", "-n", "net.ipv4.ip_forward"):
        return result(0, "1\n")
    return result()


class FakeRunner(controller_module.CommandRunner):
    def __init__(
        self,
        callback: Callable[
            [tuple[str, ...]], controller_module.CommandResult
        ]
        | None = None,
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.callback = callback

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: int = controller_module.COMMAND_TIMEOUT_SECONDS,
        journal: bool = False,
    ) -> controller_module.CommandResult:
        del timeout, journal
        command = tuple(argv)
        self.commands.append(command)
        if self.callback is not None:
            return self.callback(command)
        return default_result(command)


class ControllerFixture:
    def __init__(self, test: unittest.TestCase, *, boot_id: str = BOOT_A) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        test.addCleanup(self.temporary.cleanup)
        self.paths = controller_module.ControllerPaths(Path(self.temporary.name))
        self.boot_id = boot_id
        self.paths.state_dir.parent.mkdir(parents=True, exist_ok=True)
        self.write("/etc/machine-id", "0123456789abcdef0123456789abcdef\n", 0o444)
        self.write("/etc/ssh/ssh_host_ed25519_key", "private-key\n", 0o600)
        self.write("/etc/ssh/ssh_host_ed25519_key.pub", "public-key\n", 0o644)
        self.paths.admin_home.mkdir(parents=True)
        self.write("/etc/pi-router.conf", self.config_text(), 0o644)
        self.write("/etc/pi-router-image.conf", self.config_text(), 0o600)
        self.write(
            f"/opt/pi-router/releases/{RELEASE.name}/config/proton-repository.conf",
            'PROTON_REPO_HOST="repo.protonvpn.com"\n'
            'PROTON_REPO_PATH="/debian/dists/stable/main/binary-all"\n'
            'PROTON_REPO_PACKAGE="protonvpn-stable-release_1.0.8_all.deb"\n',
            0o644,
        )
        self.write("/sys/class/net/lan0/carrier", "1\n", 0o644)
        self.write("/sys/class/net/wan0/carrier", "1\n", 0o644)
        self.write(
            "/etc/sysctl.d/zz-pi-router-image-bootstrap.conf",
            "# Temporary image-build guard. Firstboot removes this only after setup's\n"
            "# pre-reboot checkpoint proves the canonical fail-closed firewall was applied.\n"
            "net.ipv4.ip_forward=0\n",
            0o644,
        )
        self.write(
            "/usr/sbin/policy-rc.d",
            "#!/bin/sh\n"
            "# Temporary image-owned guard: package installation during setup's first-boot\n"
            "# pre-reboot phase must not start services before setup applies router policy.\n"
            "exit 101\n",
            0o755,
        )

    @staticmethod
    def config_text() -> str:
        return """# test router configuration
ROUTER_USER="admin"
ROUTER_SESSION="rpd-labwc"
LAN_IF="lan0"
WAN_IF="wan0"
LAN_ADDR="192.168.50.1"
LAN_PREFIX="24"
DHCP_START="192.168.50.100"
DHCP_END="192.168.50.220"
DHCP_LEASE="12h"
DNS_UPSTREAM="1.1.1.3"
BYPASS_MARK="0x50564250"
BYPASS_TABLE="200"
BYPASS_RULE_PRIORITY="10000"
"""

    def write(self, absolute: str, text: str, mode: int) -> Path:
        path = self.paths.path(absolute)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")
        path.chmod(mode)
        return path

    def write_checkpoint(self, value: str, *, mode: int = 0o600) -> Path:
        return self.write(
            "/var/lib/pi-router-setup/reboot-required.boot-id", value, mode
        )

    def remove_checkpoint(self) -> None:
        try:
            self.paths.checkpoint.unlink()
        except FileNotFoundError:
            pass

    def write_full_indicator(self) -> None:
        self.write(
            "/etc/dnsmasq.d/00-pi-router-provisioning.conf",
            "# Generated temporarily by pi-router setup.\n"
            "# Give provisioning clients a LAN address only.\n"
            "# Do not advertise this Pi as router or DNS until Proton is ready.\n"
            "interface=lan0\n"
            "dhcp-range=192.168.50.100,192.168.50.220,12h\n"
            "server=1.1.1.3\n"
            "dhcp-option=3\n"
            "dhcp-option=6\n",
            0o644,
        )

    def store(self) -> image_state.ImageStateStore:
        return image_state.ImageStateStore(
            self.paths.state_dir, expected_uid=None, expected_gid=None
        )

    def controller(
        self,
        runner: FakeRunner,
        *,
        boot_id: str | None = None,
        final_service_max_failures: int = controller_module.FINAL_SERVICE_MAX_FAILURES,
    ) -> controller_module.FirstbootController:
        return controller_module.FirstbootController(
            paths=self.paths,
            runner=runner,
            store=self.store(),
            expected_uid=None,
            expected_gid=None,
            boot_id_override=boot_id or self.boot_id,
            release_override=RELEASE,
            final_service_max_failures=final_service_max_failures,
        )

    @staticmethod
    def config_attempt(*, boot_id: str = BOOT_A) -> image_state.Attempt:
        return image_state.Attempt(
            operation="config",
            invocation_boot_id=boot_id,
            checkpoint_before="absent",
            release_name=RELEASE.name,
            release_setup_sha256=RELEASE.setup_sha256,
            late_apply_before="absent",
            original_pre_boot_id="absent",
        )

    @staticmethod
    def pre_attempt(*, boot_id: str = BOOT_A) -> image_state.Attempt:
        return image_state.Attempt(
            operation="pre-full",
            invocation_boot_id=boot_id,
            checkpoint_before="absent",
            release_name=RELEASE.name,
            release_setup_sha256=RELEASE.setup_sha256,
            late_apply_before="absent",
            original_pre_boot_id="absent",
        )

    @staticmethod
    def post_attempt() -> image_state.Attempt:
        return image_state.Attempt(
            operation="post",
            invocation_boot_id=BOOT_B,
            checkpoint_before=BOOT_A,
            release_name=RELEASE.name,
            release_setup_sha256=RELEASE.setup_sha256,
            late_apply_before="not-applicable",
            original_pre_boot_id="not-applicable",
        )

    @staticmethod
    def final_attempt() -> image_state.Attempt:
        return image_state.Attempt(
            operation="final",
            invocation_boot_id=BOOT_C,
            checkpoint_before=BOOT_B,
            release_name=RELEASE.name,
            release_setup_sha256=RELEASE.setup_sha256,
            late_apply_before="not-applicable",
            original_pre_boot_id="not-applicable",
        )


class FirstbootControllerTests(unittest.TestCase):
    def test_imager_admin_contract_is_checked_before_router_setup(self) -> None:
        fixture = ControllerFixture(self)

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if command == (controller_module.FirstbootController.GETENT, "passwd", "admin"):
                return result(2)
            return default_result(command)

        runner = FakeRunner(callback)
        outcome = fixture.controller(runner).run_once()

        self.assertEqual(outcome, "terminal")
        self.assertEqual(fixture.store().read_error().code, "os-customization-failed")
        self.assertFalse(any("--phase" in command for command in runner.commands))

    def test_imager_admin_must_have_a_password_and_safe_home(self) -> None:
        fixture = ControllerFixture(self)

        def locked(command: tuple[str, ...]) -> controller_module.CommandResult:
            if command == (controller_module.FirstbootController.PASSWD, "--status", "admin"):
                return result(0, "admin L 2026-11-06 0 99999 7 -1\n")
            return default_result(command)

        with self.assertRaises(controller_module.ControllerFault) as captured:
            fixture.controller(FakeRunner(locked))._ensure_admin_account()
        self.assertEqual(captured.exception.code, "os-customization-failed")

        fixture.paths.admin_home.rmdir()
        with self.assertRaises(controller_module.ControllerFault) as captured:
            fixture.controller(FakeRunner())._ensure_admin_account()
        self.assertEqual(captured.exception.code, "os-customization-failed")

    def test_missing_lan_carrier_waits_without_pre_reboot(self) -> None:
        fixture = ControllerFixture(self)
        fixture.write("/sys/class/net/lan0/carrier", "0\n", 0o644)
        runner = FakeRunner()

        outcome = fixture.controller(runner).run_once()

        self.assertEqual(outcome, "waiting-carrier")
        self.assertEqual(fixture.store().read_status().code, "waiting-lan-carrier")
        self.assertFalse(any("pre-reboot" in command for command in runner.commands))
        self.assertEqual(fixture.store().read_state(), "new")

    def test_config_phase_is_hardware_authority_when_config_absent(self) -> None:
        fixture = ControllerFixture(self)
        fixture.paths.router_config.unlink()
        fixture.write("/sys/class/net/lan0/carrier", "0\n", 0o644)

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if "--phase" in command and command[command.index("--phase") + 1] == "config":
                attempt = fixture.store().read_attempt()
                self.assertIsNotNone(attempt)
                self.assertEqual(attempt.operation, "config")
                fixture.write("/etc/pi-router.conf", fixture.config_text(), 0o644)
                return result(0, "setup: phase config complete\n")
            return default_result(command)

        runner = FakeRunner(callback)
        fixture.controller(runner).run_once()
        calls = [command for command in runner.commands if "--phase" in command]
        self.assertEqual(
            calls,
            [
                (
                    str(fixture.paths.setup),
                    "--live",
                    "--allow-live-install",
                    "--phase",
                    "config",
                    "--answers",
                    str(fixture.paths.image_answers),
                    "--non-interactive",
                )
            ],
        )
        self.assertIsNone(fixture.store().read_attempt())

    def test_interrupted_config_safely_reruns_malformed_regular_destination(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_B)
        store = fixture.store()
        store.write_attempt(fixture.config_attempt())
        fixture.write("/etc/pi-router.conf", 'ROUTER_USER="admin"\n', 0o644)
        fixture.write("/sys/class/net/lan0/carrier", "0\n", 0o644)

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if "--phase" in command and command[command.index("--phase") + 1] == "config":
                current = store.read_attempt()
                self.assertIsNotNone(current)
                self.assertEqual(current.operation, "config")
                self.assertEqual(current.invocation_boot_id, BOOT_B)
                fixture.write("/etc/pi-router.conf", fixture.config_text(), 0o644)
                return result(0, "setup: phase config complete\n")
            return default_result(command)

        runner = FakeRunner(callback)
        self.assertEqual(fixture.controller(runner).run_once(), "waiting-carrier")
        self.assertIsNone(store.read_attempt())
        phases = [
            command[command.index("--phase") + 1]
            for command in runner.commands
            if "--phase" in command
        ]
        self.assertEqual(phases, ["config"])

    def test_interrupted_config_rejects_unsafe_destination_without_overwrite(self) -> None:
        fixture = ControllerFixture(self)
        store = fixture.store()
        store.write_attempt(fixture.config_attempt())
        unsafe = fixture.write("/etc/pi-router.conf", "partial\n", 0o600)
        runner = FakeRunner()

        self.assertEqual(fixture.controller(runner).run_once(), "terminal")
        self.assertEqual(store.read_error().code, "invalid-config")
        self.assertEqual(unsafe.read_text(encoding="ascii"), "partial\n")
        self.assertFalse(any("--phase" in command for command in runner.commands))

    def test_interrupted_config_accepts_valid_destination_without_rerun(self) -> None:
        fixture = ControllerFixture(self)
        store = fixture.store()
        store.write_attempt(fixture.config_attempt())
        fixture.write("/sys/class/net/lan0/carrier", "0\n", 0o644)
        runner = FakeRunner()

        self.assertEqual(fixture.controller(runner).run_once(), "waiting-carrier")
        self.assertIsNone(store.read_attempt())
        self.assertFalse(any("--phase" in command for command in runner.commands))

    def test_wan_bootstrap_profile_is_created_without_dhcp_route(self) -> None:
        fixture = ControllerFixture(self)
        route_ready = False

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            nonlocal route_ready
            if command[:5] == (
                "/usr/sbin/ip",
                "-4",
                "route",
                "show",
                "default",
            ):
                return default_result(command) if route_ready else result(0, "")
            if command[:3] == ("/usr/bin/nmcli", "connection", "up"):
                route_ready = True
                return result()
            if command[:2] == ("/usr/bin/getent", "ahostsv4"):
                return result(2)
            return default_result(command)

        runner = FakeRunner(callback)
        self.assertEqual(fixture.controller(runner).run_once(), "waiting-repository")
        commands = [" ".join(command) for command in runner.commands]
        self.assertTrue(
            any(
                "connection add type ethernet con-name pi-router-image-wan-bootstrap"
                in command
                for command in commands
            )
        )
        self.assertTrue(
            any(
                "connection up id pi-router-image-wan-bootstrap ifname wan0"
                in command
                for command in commands
            )
        )

    def test_repository_probe_uses_host_from_release_metadata(self) -> None:
        fixture = ControllerFixture(self)
        fixture.write(
            f"/opt/pi-router/releases/{RELEASE.name}/config/proton-repository.conf",
            '# release-owned metadata\n'
            'PROTON_REPO_HOST="packages.example.test"\n'
            'PROTON_REPO_PATH="/repository/stable"\n'
            'PROTON_REPO_PACKAGE="bootstrap_2.0_all.deb"\n',
            0o644,
        )

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if command[:2] == ("/usr/bin/getent", "ahostsv4"):
                return result(0, "198.51.100.20 STREAM packages.example.test\n")
            if command and command[0] == "/usr/bin/wget":
                return result(4)
            return default_result(command)

        runner = FakeRunner(callback)
        self.assertEqual(fixture.controller(runner).run_once(), "waiting-repository")
        self.assertIn(
            ("/usr/bin/getent", "ahostsv4", "packages.example.test"),
            runner.commands,
        )
        self.assertIn(
            (
                "/usr/bin/wget",
                "--https-only",
                "--spider",
                "--timeout=15",
                "--tries=1",
                "https://packages.example.test/repository/stable/bootstrap_2.0_all.deb",
            ),
            runner.commands,
        )

    def test_repository_probe_uses_exact_current_release_artifact(self) -> None:
        fixture = ControllerFixture(self)

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if command and command[0] == "/usr/bin/wget":
                return result(4)
            return default_result(command)

        runner = FakeRunner(callback)
        self.assertEqual(fixture.controller(runner).run_once(), "waiting-repository")
        self.assertIn(
            (
                "/usr/bin/wget",
                "--https-only",
                "--spider",
                "--timeout=15",
                "--tries=1",
                "https://repo.protonvpn.com/debian/dists/stable/main/"
                "binary-all/protonvpn-stable-release_1.0.8_all.deb",
            ),
            runner.commands,
        )

    def test_malformed_release_repository_metadata_is_terminal(self) -> None:
        invalid_documents = {
            "malicious-host": (
                'PROTON_REPO_HOST="https://attacker.invalid/path"\n'
                'PROTON_REPO_PATH="/repository/stable"\n'
                'PROTON_REPO_PACKAGE="bootstrap.deb"\n'
            ),
            "traversal-path": (
                'PROTON_REPO_HOST="packages.example.test"\n'
                'PROTON_REPO_PATH="/repository/../private"\n'
                'PROTON_REPO_PACKAGE="bootstrap.deb"\n'
            ),
            "traversal-package": (
                'PROTON_REPO_HOST="packages.example.test"\n'
                'PROTON_REPO_PATH="/repository/stable"\n'
                'PROTON_REPO_PACKAGE="../bootstrap.deb"\n'
            ),
            "missing-package": (
                'PROTON_REPO_HOST="packages.example.test"\n'
                'PROTON_REPO_PATH="/repository/stable"\n'
            ),
            "duplicate-host": (
                'PROTON_REPO_HOST="packages.example.test"\n'
                'PROTON_REPO_HOST="mirror.example.test"\n'
                'PROTON_REPO_PATH="/repository/stable"\n'
                'PROTON_REPO_PACKAGE="bootstrap.deb"\n'
            ),
        }
        for label, document in invalid_documents.items():
            with self.subTest(label=label):
                fixture = ControllerFixture(self)
                fixture.write(
                    f"/opt/pi-router/releases/{RELEASE.name}/config/proton-repository.conf",
                    document,
                    0o644,
                )

                self.assertEqual(
                    fixture.controller(FakeRunner()).run_once(), "terminal"
                )
                self.assertEqual(
                    fixture.store().read_error().code, "release-integrity"
                )

    def test_pre_success_commits_removes_guard_and_requests_reboot(self) -> None:
        fixture = ControllerFixture(self)

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if (
                "--phase" in command
                and command[command.index("--phase") + 1] == "pre-reboot"
            ):
                fixture.write_checkpoint(f"{BOOT_A}\n")
                return result(
                    0,
                    "setup: pre-reboot stage complete\n"
                    "setup: REBOOT REQUIRED before post-reboot\n",
                )
            return default_result(command)

        runner = FakeRunner(callback)
        outcome = fixture.controller(runner).run_once()
        store = fixture.store()
        self.assertEqual(outcome, "reboot-requested")
        self.assertEqual(store.read_state(), "pre-reboot-complete")
        self.assertIsNone(store.read_attempt())
        self.assertFalse(fixture.paths.forwarding_guard.exists())
        self.assertFalse(fixture.paths.package_service_guard.exists())
        self.assertIn(("/usr/bin/systemctl", "--no-block", "reboot"), runner.commands)
        self.assertIn(
            (
                str(fixture.paths.setup),
                "--live",
                "--allow-live-install",
                "--phase",
                "pre-reboot",
                "--answers",
                str(fixture.paths.router_config),
                "--allow-network-cutover",
                "--allow-wifi-disable",
                "--non-interactive",
            ),
            runner.commands,
        )
        # The timer must survive this reboot so firstboot runs again on the
        # next boot; onboarding has not started yet, so there is nothing to
        # stop here.
        self.assertNotIn(
            (
                "/usr/bin/systemctl",
                "stop",
                "pi-router-onboarding.service",
                "pi-router-onboarding-backend.service",
            ),
            runner.commands,
        )
        self.assertNotIn(
            (
                "/usr/bin/systemctl",
                "disable",
                "--now",
                "pi-router-firstboot.timer",
            ),
            runner.commands,
        )

    def test_pre_success_accepts_already_removed_package_service_guard(self) -> None:
        fixture = ControllerFixture(self)
        fixture.paths.package_service_guard.unlink()

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if (
                "--phase" in command
                and command[command.index("--phase") + 1] == "pre-reboot"
            ):
                fixture.write_checkpoint(f"{BOOT_A}\n")
                return result(
                    0,
                    "setup: pre-reboot stage complete\n"
                    "setup: REBOOT REQUIRED before post-reboot\n",
                )
            return default_result(command)

        self.assertEqual(
            fixture.controller(FakeRunner(callback)).run_once(), "reboot-requested"
        )
        self.assertEqual(fixture.store().read_state(), "pre-reboot-complete")

    def test_imager_selected_ssh_starts_only_after_firewall_cutover(self) -> None:
        fixture = ControllerFixture(self)

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if (
                "--phase" in command
                and command[command.index("--phase") + 1] == "pre-reboot"
            ):
                fixture.write_checkpoint(f"{BOOT_A}\n")
                return result(
                    0,
                    "setup: pre-reboot stage complete\n"
                    "setup: REBOOT REQUIRED before post-reboot\n",
                )
            if command == ("/usr/bin/systemctl", "is-enabled", "ssh.service"):
                return result(0, "enabled\n")
            return default_result(command)

        runner = FakeRunner(callback)
        self.assertEqual(fixture.controller(runner).run_once(), "reboot-requested")
        restart = ("/usr/bin/systemctl", "restart", "ssh.service")
        self.assertIn(restart, runner.commands)
        firewall = ("/usr/sbin/nft", "list", "table", "inet", "router_filter")
        self.assertLess(runner.commands.index(firewall), runner.commands.index(restart))

    def test_changed_package_service_guard_fails_closed(self) -> None:
        fixture = ControllerFixture(self)
        fixture.paths.package_service_guard.write_text(
            "#!/bin/sh\nexit 0\n", encoding="ascii"
        )

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if (
                "--phase" in command
                and command[command.index("--phase") + 1] == "pre-reboot"
            ):
                fixture.write_checkpoint(f"{BOOT_A}\n")
                return result(
                    0,
                    "setup: pre-reboot stage complete\n"
                    "setup: REBOOT REQUIRED before post-reboot\n",
                )
            return default_result(command)

        outcome = fixture.controller(FakeRunner(callback)).run_once()

        self.assertEqual(outcome, "terminal")
        self.assertEqual(fixture.store().read_state(), "new")
        self.assertEqual(fixture.store().read_error().code, "setup-pre-failed")
        self.assertTrue(fixture.paths.package_service_guard.exists())

    def test_failure_before_pre_attempt_write_never_invokes_setup(self) -> None:
        fixture = ControllerFixture(self)
        controller = fixture.controller(FakeRunner())

        with mock.patch.object(
            controller.store,
            "write_attempt",
            side_effect=image_state.StateDurabilityError(
                "injected crash before attempt write"
            ),
        ):
            with self.assertRaises(image_state.StateDurabilityError):
                controller.run_once()

        self.assertEqual(fixture.store().read_state(), "new")
        self.assertIsNone(fixture.store().read_attempt())
        self.assertFalse(
            any(
                "--phase" in command
                and command[command.index("--phase") + 1] == "pre-reboot"
                for command in controller.runner.commands
            )
        )

    def test_failure_after_pre_attempt_write_is_durably_fail_closed(self) -> None:
        fixture = ControllerFixture(self)
        controller = fixture.controller(FakeRunner())
        real_write_attempt = controller.store.write_attempt

        def write_then_fail(attempt: image_state.Attempt) -> image_state.Attempt:
            real_write_attempt(attempt)
            raise image_state.StateDurabilityError(
                "injected crash after attempt write"
            )

        with mock.patch.object(
            controller.store,
            "write_attempt",
            side_effect=write_then_fail,
        ):
            with self.assertRaises(image_state.StateDurabilityError):
                controller.run_once()

        self.assertEqual(fixture.store().read_state(), "new")
        self.assertEqual(fixture.store().read_attempt(), fixture.pre_attempt())
        self.assertFalse(
            any(
                "--phase" in command
                and command[command.index("--phase") + 1] == "pre-reboot"
                for command in controller.runner.commands
            )
        )

        self.assertEqual(fixture.controller(FakeRunner()).run_once(), "terminal")
        self.assertEqual(fixture.store().read_error().code, "resume-unsafe")
        self.assertIsNone(fixture.store().read_ready())

    def test_pre_checkpoint_reconciles_lost_result_after_power_cycle(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_B)
        store = fixture.store()
        store.write_attempt(fixture.pre_attempt(boot_id=BOOT_A))
        fixture.write_checkpoint(f"{BOOT_A}\n")
        runner = FakeRunner()

        outcome = fixture.controller(runner).run_once()

        self.assertEqual(outcome, "waiting-onboarding")
        self.assertEqual(store.read_state(), "pre-reboot-complete")
        self.assertIn(
            ("/usr/bin/systemctl", "start", "pi-router-onboarding.service"),
            runner.commands,
        )
        self.assertFalse(any("--phase" in command for command in runner.commands))

    def test_pre_checkpoint_is_authoritative_when_process_output_is_lost(self) -> None:
        fixture = ControllerFixture(self)

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if "--phase" in command and command[command.index("--phase") + 1] == "pre-reboot":
                fixture.write_checkpoint(f"{BOOT_A}\n")
                return result(124, "")
            return default_result(command)

        outcome = fixture.controller(FakeRunner(callback)).run_once()

        self.assertEqual(outcome, "reboot-requested")
        self.assertEqual(fixture.store().read_state(), "pre-reboot-complete")
        self.assertIsNone(fixture.store().read_error())

    def test_exact_indicator_authorizes_pre_resume(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_B)
        fixture.write_full_indicator()
        fixture.store().write_attempt(fixture.pre_attempt(boot_id=BOOT_A))

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if "--phase" in command and command[command.index("--phase") + 1] == "pre-reboot-resume":
                fixture.write_checkpoint(f"{BOOT_B}\n")
                return result(
                    0,
                    "setup: pre-reboot stage complete\n"
                    "setup: REBOOT REQUIRED before post-reboot\n",
                )
            return default_result(command)

        runner = FakeRunner(callback)
        self.assertEqual(fixture.controller(runner).run_once(), "reboot-requested")
        phases = [
            command[command.index("--phase") + 1]
            for command in runner.commands
            if "--phase" in command
        ]
        self.assertEqual(phases, ["pre-reboot-resume"])
        self.assertIn(
            (
                str(fixture.paths.setup),
                "--live",
                "--allow-live-install",
                "--phase",
                "pre-reboot-resume",
                "--answers",
                str(fixture.paths.router_config),
                "--allow-network-cutover",
                "--allow-wifi-disable",
                "--non-interactive",
            ),
            runner.commands,
        )
        self.assertEqual(fixture.store().read_state(), "pre-reboot-complete")

    def test_interrupted_pre_without_indicator_is_terminal(self) -> None:
        fixture = ControllerFixture(self)
        store = fixture.store()
        store.write_attempt(fixture.pre_attempt())

        self.assertEqual(fixture.controller(FakeRunner()).run_once(), "terminal")
        self.assertEqual(store.read_state(), "new")
        self.assertEqual(store.read_error().code, "resume-unsafe")
        self.assertIsNone(store.read_ready())

    def test_malformed_checkpoint_is_terminal_and_not_rewritten(self) -> None:
        fixture = ControllerFixture(self)
        store = fixture.store()
        store.write_state("pre-reboot-complete")
        checkpoint = fixture.write_checkpoint("not-a-boot-id\n")
        runner = FakeRunner()

        self.assertEqual(fixture.controller(runner).run_once(), "terminal")
        self.assertEqual(checkpoint.read_text(encoding="ascii"), "not-a-boot-id\n")
        self.assertEqual(store.read_error().code, "checkpoint-corrupt")
        self.assertFalse(any("--phase" in command for command in runner.commands))

    def test_first_terminal_error_keeps_timer_when_cleanup_fails(self) -> None:
        fixture = ControllerFixture(self)
        store = fixture.store()
        fixture.write_checkpoint("not-a-boot-id\n")

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if command[:2] == ("/usr/bin/systemctl", "stop"):
                return result(1)
            if command == (
                "/usr/bin/systemctl",
                "is-active",
                "pi-router-onboarding.service",
            ):
                return result(0, "active\n")
            return default_result(command)

        runner = FakeRunner(callback)
        self.assertEqual(
            fixture.controller(runner).run_once(),
            "terminal-cleanup-pending",
        )
        self.assertEqual(store.read_error().code, "checkpoint-corrupt")
        self.assertEqual(store.read_status().code, "error")
        self.assertNotIn(
            (
                "/usr/bin/systemctl",
                "disable",
                "--now",
                "pi-router-firstboot.timer",
            ),
            runner.commands,
        )

    def test_existing_terminal_error_retries_cleanup_before_disabling_timer(
        self,
    ) -> None:
        fixture = ControllerFixture(self)
        store = fixture.store()
        store.write_error("new", "invalid-state")

        def failed_cleanup(
            command: tuple[str, ...],
        ) -> controller_module.CommandResult:
            if command[:2] == ("/usr/bin/systemctl", "stop"):
                return result(1)
            if command == (
                "/usr/bin/systemctl",
                "is-active",
                "pi-router-onboarding.service",
            ):
                return result(0, "active\n")
            return default_result(command)

        first_runner = FakeRunner(failed_cleanup)
        self.assertEqual(
            fixture.controller(first_runner).run_once(),
            "terminal-cleanup-pending",
        )
        self.assertEqual(store.read_error().code, "invalid-state")
        self.assertNotIn(
            (
                "/usr/bin/systemctl",
                "disable",
                "--now",
                "pi-router-firstboot.timer",
            ),
            first_runner.commands,
        )

        retry_runner = FakeRunner()
        self.assertEqual(fixture.controller(retry_runner).run_once(), "terminal")
        self.assertEqual(store.read_error().code, "invalid-state")
        self.assertIn(
            (
                "/usr/bin/systemctl",
                "disable",
                "--now",
                "pi-router-firstboot.timer",
            ),
            retry_runner.commands,
        )

    def test_post_checkpoint_commit_advances_and_requests_reboot(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_B)
        store = fixture.store()
        store.write_state("pre-reboot-complete")
        store.write_attempt(fixture.post_attempt())
        fixture.write_checkpoint(f"{BOOT_B}\n")
        runner = FakeRunner()

        self.assertEqual(fixture.controller(runner).run_once(), "reboot-requested")
        self.assertEqual(store.read_state(), "post-reboot-complete")
        self.assertIsNone(store.read_attempt())
        self.assertIsNone(store.read_ready())
        self.assertFalse(
            any(
                "--phase" in command
                and command[command.index("--phase") + 1] == "final-verify"
                for command in runner.commands
            )
        )

    def test_failed_reboot_request_is_retried_from_durable_pre_state(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_A)
        store = fixture.store()
        store.write_state("pre-reboot-complete")
        fixture.write_checkpoint(f"{BOOT_A}\n")

        def fail_reboot(command: tuple[str, ...]) -> controller_module.CommandResult:
            if command == ("/usr/bin/systemctl", "--no-block", "reboot"):
                return result(1)
            return default_result(command)

        first_runner = FakeRunner(fail_reboot)
        self.assertEqual(
            fixture.controller(first_runner).run_once(),
            "reboot-pending",
        )
        self.assertEqual(store.read_state(), "pre-reboot-complete")
        self.assertEqual(store.read_status().code, "rebooting-after-pre")

        retry_runner = FakeRunner()
        self.assertEqual(
            fixture.controller(retry_runner).run_once(),
            "reboot-requested",
        )
        self.assertFalse(any("--phase" in command for command in retry_runner.commands))

    def test_uncommitted_post_returns_to_onboarding(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_B)
        store = fixture.store()
        store.write_state("pre-reboot-complete")
        store.write_attempt(fixture.post_attempt())
        fixture.write_checkpoint(f"{BOOT_A}\n")

        self.assertEqual(
            fixture.controller(FakeRunner()).run_once(), "waiting-onboarding"
        )
        self.assertEqual(store.read_state(), "pre-reboot-complete")
        self.assertIsNone(store.read_attempt())

    def test_onboarding_start_failure_persists_waiting_desktop(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_B)
        store = fixture.store()
        store.write_state("pre-reboot-complete")
        fixture.write_checkpoint(f"{BOOT_A}\n")

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if command == (
                "/usr/bin/systemctl",
                "start",
                "pi-router-onboarding.service",
            ):
                return result(1)
            return default_result(command)

        self.assertEqual(
            fixture.controller(FakeRunner(callback)).run_once(), "waiting-desktop"
        )
        self.assertEqual(store.read_status().code, "waiting-desktop")

    def test_final_success_consumes_checkpoint_and_creates_ready(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_C)
        store = fixture.store()
        store.write_state("post-reboot-complete")
        fixture.write_checkpoint(f"{BOOT_B}\n")

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if "--phase" in command and command[command.index("--phase") + 1] == "final-verify":
                fixture.remove_checkpoint()
                return result(0, "setup: FINAL VERIFICATION PASSED\n")
            return default_result(command)

        runner = FakeRunner(callback)
        self.assertEqual(fixture.controller(runner).run_once(), "ready")
        self.assertEqual(store.read_state(), "ready")
        self.assertEqual(store.read_ready().proof, "exit-zero-and-marker")
        self.assertIsNone(store.read_attempt())
        self.assertIsNone(store.read_final_wait())
        self.assertIn(
            (
                str(fixture.paths.setup),
                "--live",
                "--allow-live-install",
                "--phase",
                "final-verify",
                "--non-interactive",
            ),
            runner.commands,
        )

    def test_ready_cleanup_failure_keeps_timer_for_a_retry(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_C)
        store = fixture.store()
        store.write_state("post-reboot-complete")
        fixture.write_checkpoint(f"{BOOT_B}\n")

        def first_callback(
            command: tuple[str, ...],
        ) -> controller_module.CommandResult:
            if (
                "--phase" in command
                and command[command.index("--phase") + 1] == "final-verify"
            ):
                fixture.remove_checkpoint()
                return result(0, "setup: FINAL VERIFICATION PASSED\n")
            if command[:2] == ("/usr/bin/systemctl", "stop"):
                return result(1)
            if command == (
                "/usr/bin/systemctl",
                "is-active",
                "pi-router-onboarding.service",
            ):
                return result(0, "active\n")
            return default_result(command)

        first_runner = FakeRunner(first_callback)
        self.assertEqual(
            fixture.controller(first_runner).run_once(),
            "ready-cleanup-pending",
        )
        self.assertEqual(store.read_state(), "ready")
        self.assertIsNotNone(store.read_ready())
        self.assertNotIn(
            (
                "/usr/bin/systemctl",
                "disable",
                "--now",
                "pi-router-firstboot.timer",
            ),
            first_runner.commands,
        )

        retry_runner = FakeRunner()
        self.assertEqual(fixture.controller(retry_runner).run_once(), "ready")
        self.assertIn(
            (
                "/usr/bin/systemctl",
                "disable",
                "--now",
                "pi-router-firstboot.timer",
            ),
            retry_runner.commands,
        )

    def test_failed_stop_with_confirmed_inactive_units_can_finish_cleanup(
        self,
    ) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_C)
        store = fixture.store()
        store.write_state("ready")
        store.write_ready(RELEASE.name, BOOT_C, "exit-zero-and-marker")

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if command[:2] == ("/usr/bin/systemctl", "stop"):
                return result(1)
            if command[:2] == ("/usr/bin/systemctl", "is-active"):
                return result(3, "inactive\n")
            return default_result(command)

        runner = FakeRunner(callback)
        self.assertEqual(fixture.controller(runner).run_once(), "ready")
        self.assertIn(
            (
                "/usr/bin/systemctl",
                "disable",
                "--now",
                "pi-router-firstboot.timer",
            ),
            runner.commands,
        )

    def test_final_success_does_not_ready_while_checkpoint_remains(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_C)
        store = fixture.store()
        store.write_state("post-reboot-complete")
        fixture.write_checkpoint(f"{BOOT_B}\n")

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if (
                "--phase" in command
                and command[command.index("--phase") + 1] == "final-verify"
            ):
                return result(0, "setup: FINAL VERIFICATION PASSED\n")
            return default_result(command)

        self.assertEqual(
            fixture.controller(FakeRunner(callback)).run_once(), "terminal"
        )
        self.assertEqual(store.read_error().code, "setup-final-failed")
        self.assertEqual(store.read_state(), "post-reboot-complete")
        self.assertIsNone(store.read_ready())

    def test_final_waits_until_every_required_service_is_active(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_C)
        store = fixture.store()
        store.write_state("post-reboot-complete")
        fixture.write_checkpoint(f"{BOOT_B}\n")

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if command == (
                "/usr/bin/systemctl",
                "is-active",
                "--quiet",
                "dnsmasq.service",
            ):
                return result(3)
            return default_result(command)

        runner = FakeRunner(callback)
        self.assertEqual(
            fixture.controller(runner).run_once(), "waiting-final-services"
        )
        self.assertEqual(store.read_status().code, "waiting-final-services")
        self.assertEqual(
            store.read_final_wait(), image_state.FinalServiceWait(BOOT_C, 1)
        )
        self.assertFalse(any("--phase" in command for command in runner.commands))

    def test_final_service_wait_is_bounded_and_terminal(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_C)
        store = fixture.store()
        store.write_state("post-reboot-complete")
        fixture.write_checkpoint(f"{BOOT_B}\n")

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if command == (
                "/usr/bin/systemctl",
                "is-active",
                "--quiet",
                "lightdm.service",
            ):
                return result(3)
            return default_result(command)

        controller = fixture.controller(
            FakeRunner(callback), final_service_max_failures=2
        )
        self.assertEqual(controller.run_once(), "waiting-final-services")
        self.assertEqual(
            store.read_final_wait(), image_state.FinalServiceWait(BOOT_C, 1)
        )

        self.assertEqual(
            fixture.controller(
                FakeRunner(callback), final_service_max_failures=2
            ).run_once(),
            "terminal",
        )
        self.assertEqual(store.read_error().code, "final-services-timeout")
        self.assertEqual(store.read_state(), "post-reboot-complete")
        self.assertIsNone(store.read_ready())
        self.assertEqual(
            store.read_final_wait(), image_state.FinalServiceWait(BOOT_C, 2)
        )

    def test_final_service_wait_resets_for_a_different_boot(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_C)
        store = fixture.store()
        store.write_state("post-reboot-complete")
        store.write_final_wait(image_state.FinalServiceWait(BOOT_B, 39))
        fixture.write_checkpoint(f"{BOOT_A}\n")

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if command == (
                "/usr/bin/systemctl",
                "is-active",
                "--quiet",
                "lightdm.service",
            ):
                return result(3)
            return default_result(command)

        self.assertEqual(
            fixture.controller(FakeRunner(callback)).run_once(),
            "waiting-final-services",
        )
        self.assertEqual(
            store.read_final_wait(), image_state.FinalServiceWait(BOOT_C, 1)
        )

    def test_current_final_failure_after_checkpoint_consumption_needs_recovery_pass(
        self,
    ) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_C)
        store = fixture.store()
        store.write_state("post-reboot-complete")
        fixture.write_checkpoint(f"{BOOT_B}\n")

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if "--phase" in command and command[command.index("--phase") + 1] == "final-verify":
                fixture.remove_checkpoint()
                return result(1, "final verification output was interrupted\n")
            return default_result(command)

        self.assertEqual(
            fixture.controller(FakeRunner(callback)).run_once(),
            "waiting-final-recovery",
        )
        self.assertEqual(store.read_state(), "post-reboot-complete")
        self.assertIsNone(store.read_ready())
        self.assertEqual(store.read_attempt(), fixture.final_attempt())
        self.assertEqual(store.read_status().code, "waiting-final-recovery")

        self.assertEqual(fixture.controller(FakeRunner()).run_once(), "ready")
        self.assertEqual(store.read_state(), "ready")
        self.assertEqual(
            store.read_ready().proof,
            "checkpoint-consumed-after-durable-intent",
        )

    def test_current_final_missing_marker_after_consumption_needs_recovery_pass(
        self,
    ) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_C)
        store = fixture.store()
        store.write_state("post-reboot-complete")
        fixture.write_checkpoint(f"{BOOT_B}\n")

        def callback(command: tuple[str, ...]) -> controller_module.CommandResult:
            if "--phase" in command and command[command.index("--phase") + 1] == "final-verify":
                fixture.remove_checkpoint()
                return result(0, "phase final-verify complete\n")
            return default_result(command)

        self.assertEqual(
            fixture.controller(FakeRunner(callback)).run_once(),
            "waiting-final-recovery",
        )
        self.assertEqual(store.read_state(), "post-reboot-complete")
        self.assertIsNone(store.read_ready())
        self.assertIsNotNone(store.read_attempt())

    def test_consumed_checkpoint_with_final_attempt_reconciles_ready(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_A)
        store = fixture.store()
        store.write_state("post-reboot-complete")
        store.write_attempt(fixture.final_attempt())

        self.assertEqual(fixture.controller(FakeRunner()).run_once(), "ready")
        self.assertEqual(store.read_state(), "ready")
        self.assertEqual(
            store.read_ready().proof,
            "checkpoint-consumed-after-durable-intent",
        )
        self.assertEqual(store.read_ready().completed_boot_id, BOOT_C)

    def test_crash_after_ready_receipt_write_reconciles_on_restart(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_A)
        store = fixture.store()
        store.write_state("post-reboot-complete")
        store.write_attempt(fixture.final_attempt())
        controller = fixture.controller(FakeRunner())
        real_write_ready = controller.store.write_ready

        def write_then_fail(
            release_name: str,
            completed_boot_id: str,
            proof: str,
        ) -> image_state.ReadyReceipt:
            real_write_ready(release_name, completed_boot_id, proof)
            raise image_state.StateDurabilityError(
                "injected crash after ready receipt write"
            )

        with mock.patch.object(
            controller.store,
            "write_ready",
            side_effect=write_then_fail,
        ):
            with self.assertRaises(image_state.StateDurabilityError):
                controller.run_once()

        self.assertEqual(store.read_state(), "post-reboot-complete")
        self.assertIsNotNone(store.read_ready())
        self.assertEqual(store.read_attempt(), fixture.final_attempt())

        self.assertEqual(fixture.controller(FakeRunner()).run_once(), "ready")
        self.assertEqual(store.read_state(), "ready")
        self.assertIsNotNone(store.read_ready())
        self.assertIsNone(store.read_attempt())

    def test_crash_before_ready_state_write_reconciles_on_restart(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_A)
        store = fixture.store()
        store.write_state("post-reboot-complete")
        store.write_attempt(fixture.final_attempt())
        controller = fixture.controller(FakeRunner())

        with mock.patch.object(
            controller.store,
            "write_state",
            side_effect=image_state.StateDurabilityError(
                "injected crash before ready state write"
            ),
        ):
            with self.assertRaises(image_state.StateDurabilityError):
                controller.run_once()

        self.assertEqual(store.read_state(), "post-reboot-complete")
        self.assertIsNotNone(store.read_ready())
        self.assertEqual(store.read_attempt(), fixture.final_attempt())

        self.assertEqual(fixture.controller(FakeRunner()).run_once(), "ready")
        self.assertEqual(store.read_state(), "ready")
        self.assertIsNone(store.read_attempt())

    def test_crash_after_ready_state_write_reconciles_attempt_cleanup(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_A)
        store = fixture.store()
        store.write_state("post-reboot-complete")
        store.write_attempt(fixture.final_attempt())
        controller = fixture.controller(FakeRunner())
        real_write_state = controller.store.write_state

        def write_then_fail(state: str) -> str:
            real_write_state(state)
            raise image_state.StateDurabilityError(
                "injected crash after ready state write"
            )

        with mock.patch.object(
            controller.store,
            "write_state",
            side_effect=write_then_fail,
        ):
            with self.assertRaises(image_state.StateDurabilityError):
                controller.run_once()

        self.assertEqual(store.read_state(), "ready")
        self.assertIsNotNone(store.read_ready())
        self.assertEqual(store.read_attempt(), fixture.final_attempt())

        self.assertEqual(fixture.controller(FakeRunner()).run_once(), "ready")
        self.assertEqual(store.read_state(), "ready")
        self.assertIsNone(store.read_attempt())

    def test_missing_final_checkpoint_without_attempt_is_terminal(self) -> None:
        fixture = ControllerFixture(self, boot_id=BOOT_C)
        store = fixture.store()
        store.write_state("post-reboot-complete")

        self.assertEqual(fixture.controller(FakeRunner()).run_once(), "terminal")
        self.assertEqual(store.read_error().code, "checkpoint-missing")
        self.assertIsNone(store.read_ready())


class UnitContractTests(unittest.TestCase):
    def test_state_and_frontend_status_schemas_match(self) -> None:
        self.assertEqual(
            image_state.STATUS_CODES,
            onboarding_frontend.STATUS_CODES,
        )
        self.assertEqual(
            image_state.STATUS_CODES,
            frozenset(onboarding_frontend._STATUS_PRESENTATION),
        )

    def test_all_controller_terminal_codes_exist_in_state_schema(self) -> None:
        source = (FIRSTBOOT_DIR / "pi_router_image_controller.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        codes: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in {"ControllerFault", "CheckpointFault"}:
                continue
            if isinstance(node.args[0], ast.Constant) and isinstance(
                node.args[0].value, str
            ):
                codes.add(node.args[0].value)
        self.assertTrue(codes)
        self.assertEqual(codes - image_state.ERROR_CODES, set())

    def test_timer_retries_oneshot_without_network_online_dependency(self) -> None:
        service = (FIRSTBOOT_DIR / "pi-router-firstboot.service").read_text(
            encoding="utf-8"
        )
        timer = (FIRSTBOOT_DIR / "pi-router-firstboot.timer").read_text(
            encoding="utf-8"
        )
        self.assertIn("Type=oneshot", service)
        self.assertNotIn("RemainAfterExit", service)
        self.assertNotIn("network-online.target", service)
        self.assertIn("Wants=cloud-init.target", service)
        self.assertIn(
            "After=local-fs.target cloud-init.target systemd-machine-id-commit.service systemd-sysusers.service systemd-tmpfiles-setup.service NetworkManager.service",
            service,
        )
        self.assertIn("ExecStart=/usr/local/libexec/pi-router-firstboot", service)
        self.assertIn("TimeoutStartSec=40min", service)
        self.assertIn("OnUnitInactiveSec=15s", timer)
        self.assertIn("WantedBy=timers.target", timer)


if __name__ == "__main__":
    unittest.main()
