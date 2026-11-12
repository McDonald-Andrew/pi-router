#!/usr/bin/env python3
"""Resumable provisioning controller for the Pi Router image.

This module deliberately treats the versioned ``setup`` executable as the
authority for router installation.  It orchestrates public setup phases and
owns only image lifecycle concerns: clone identity, connectivity gating,
durable attempts/state, reboot boundaries, and the temporary forwarding
guard.

Production paths are fixed.  ``ControllerPaths`` and ``CommandRunner`` make
the filesystem root and external command execution replaceable in unit tests;
they are not command-line configuration surfaces in the installed appliance.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import ipaddress
import os
import re
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable, Final, Iterator, Mapping, Sequence

from pi_router_image_state import (
    Attempt,
    FinalServiceWait,
    ImageStateError,
    ImageStateStore,
    StateFormatError,
    validate_boot_id,
    validate_release_name,
    validate_sha256,
)

try:  # Linux production dependency; tests can call run_once without locking.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts.
    fcntl = None  # type: ignore[assignment]


SETUP_TIMEOUT_SECONDS: Final = 30 * 60
COMMAND_TIMEOUT_SECONDS: Final = 60
MAX_COMMAND_OUTPUT: Final = 1024 * 1024
# The timer normally runs every 15 seconds, so forty failed readiness checks
# allow roughly ten minutes for the graphical session and Proton autoconnect.
# Persisting the count makes the bound survive controller restarts.
FINAL_SERVICE_MAX_FAILURES: Final = 40
BOOTSTRAP_CONNECTION: Final = "pi-router-image-wan-bootstrap"
PROTON_REPOSITORY_KEYS: Final = (
    "PROTON_REPO_HOST",
    "PROTON_REPO_PATH",
    "PROTON_REPO_PACKAGE",
)

PRE_MARKERS: Final = (
    "pre-reboot stage complete",
    "REBOOT REQUIRED before post-reboot",
)
FINAL_MARKER: Final = "FINAL VERIFICATION PASSED"

CONFIG_KEYS: Final = (
    "ROUTER_USER",
    "ROUTER_SESSION",
    "LAN_IF",
    "WAN_IF",
    "LAN_ADDR",
    "LAN_PREFIX",
    "DHCP_START",
    "DHCP_END",
    "DHCP_LEASE",
    "DNS_UPSTREAM",
    "BYPASS_MARK",
    "BYPASS_TABLE",
    "BYPASS_RULE_PRIORITY",
)
INTERFACE_RE: Final = re.compile(r"[A-Za-z0-9_.:-]+")
ASSIGNMENT_RE: Final = re.compile(r'([A-Z][A-Z0-9_]*)="([^"\r\n]*)"')
MACHINE_ID_RE: Final = re.compile(r"[0-9a-f]{32}")
DNS_HOSTNAME_RE: Final = re.compile(
    r"(?=.{1,253}\Z)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
REPOSITORY_PATH_RE: Final = re.compile(
    r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*"
)
REPOSITORY_PACKAGE_RE: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+-]*\.deb"
)


@dataclasses.dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    output: str = ""


class CommandRunner:
    """Run argv-only commands and optionally mirror bounded output to journal."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: int = COMMAND_TIMEOUT_SECONDS,
        journal: bool = False,
    ) -> CommandResult:
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                tuple(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            payload, _ = process.communicate(timeout=timeout)
            payload = payload[: MAX_COMMAND_OUTPUT + 1]
            output = payload.decode("utf-8", errors="replace")
            if len(payload) > MAX_COMMAND_OUTPUT:
                output = output[:MAX_COMMAND_OUTPUT] + "\n[output truncated]\n"
            if journal and output:
                print(output, end="" if output.endswith("\n") else "\n", flush=True)
            return CommandResult(process.returncode, output)
        except subprocess.TimeoutExpired as exc:
            if process is None:  # Defensive; Popen failures raise OSError.
                payload = exc.stdout or b""
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (AttributeError, ProcessLookupError, OSError):
                    try:
                        process.terminate()
                    except OSError:
                        pass
                try:
                    payload, _ = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (AttributeError, ProcessLookupError, OSError):
                        try:
                            process.kill()
                        except OSError:
                            pass
                    payload, _ = process.communicate()
            if isinstance(payload, str):
                output = payload[:MAX_COMMAND_OUTPUT]
            else:
                output = payload[:MAX_COMMAND_OUTPUT].decode(
                    "utf-8", errors="replace"
                )
            if journal and output:
                print(output, end="" if output.endswith("\n") else "\n", flush=True)
            return CommandResult(124, output)
        except OSError as exc:
            return CommandResult(127, f"command execution failed: {exc}\n")


@dataclasses.dataclass(frozen=True, slots=True)
class ControllerPaths:
    root: Path = Path("/")

    def __post_init__(self) -> None:
        if not self.root.is_absolute():
            raise ValueError("controller root must be absolute")

    def path(self, absolute: str) -> Path:
        if not absolute.startswith("/"):
            raise ValueError(f"expected absolute appliance path: {absolute!r}")
        if self.root == Path("/"):
            return Path(absolute)
        return self.root / absolute.removeprefix("/")

    @property
    def setup(self) -> Path:
        return self.path("/opt/pi-router/current/setup")

    @property
    def state_dir(self) -> Path:
        return self.path("/var/lib/pi-router-image")

    @property
    def checkpoint(self) -> Path:
        return self.path("/var/lib/pi-router-setup/reboot-required.boot-id")

    @property
    def router_config(self) -> Path:
        return self.path("/etc/pi-router.conf")

    @property
    def image_answers(self) -> Path:
        return self.path("/etc/pi-router-image.conf")

    @property
    def provisioning_indicator(self) -> Path:
        return self.path("/etc/dnsmasq.d/00-pi-router-provisioning.conf")

    @property
    def forwarding_guard(self) -> Path:
        return self.path("/etc/sysctl.d/zz-pi-router-image-bootstrap.conf")

    @property
    def package_service_guard(self) -> Path:
        return self.path("/usr/sbin/policy-rc.d")

    @property
    def metadata(self) -> Path:
        return self.path("/usr/share/pi-router/image-build/metadata")

    @property
    def boot_id(self) -> Path:
        return self.path("/proc/sys/kernel/random/boot_id")

    @property
    def machine_id(self) -> Path:
        return self.path("/etc/machine-id")

    @property
    def ssh_dir(self) -> Path:
        return self.path("/etc/ssh")

    @property
    def admin_home(self) -> Path:
        return self.path("/home/admin")

    @property
    def lock(self) -> Path:
        return self.path("/run/lock/pi-router-image-controller.lock")


@dataclasses.dataclass(frozen=True, slots=True)
class ReleaseInfo:
    name: str
    setup_sha256: str

    def __post_init__(self) -> None:
        validate_release_name(self.name)
        validate_sha256(self.setup_sha256)


class ControllerFault(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CheckpointFault(ControllerFault):
    pass


class LockUnavailable(RuntimeError):
    pass


def _mode(metadata: os.stat_result) -> int:
    return stat.S_IMODE(metadata.st_mode)


class ProvisioningController:
    """One idempotent pass through the image provisioning state machine."""

    SYSTEMCTL: Final = "/usr/bin/systemctl"
    NMCLI: Final = "/usr/bin/nmcli"
    IP: Final = "/usr/sbin/ip"
    NFT: Final = "/usr/sbin/nft"
    SYSCTL: Final = "/usr/sbin/sysctl"
    GETENT: Final = "/usr/bin/getent"
    WGET: Final = "/usr/bin/wget"
    SSH_KEYGEN: Final = "/usr/bin/ssh-keygen"
    PASSWD: Final = "/usr/bin/passwd"
    MACHINE_ID_SETUP: Final = "/usr/bin/systemd-machine-id-setup"

    def __init__(
        self,
        *,
        paths: ControllerPaths | None = None,
        runner: CommandRunner | None = None,
        store: ImageStateStore | None = None,
        expected_uid: int | None = 0,
        expected_gid: int | None = 0,
        boot_id_override: str | None = None,
        release_override: ReleaseInfo | None = None,
        final_service_max_failures: int = FINAL_SERVICE_MAX_FAILURES,
    ) -> None:
        self.paths = paths or ControllerPaths()
        self.runner = runner or CommandRunner()
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self.store = store or ImageStateStore(
            self.paths.state_dir,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        self.boot_id_override = boot_id_override
        self.release_override = release_override
        if (
            isinstance(final_service_max_failures, bool)
            or not isinstance(final_service_max_failures, int)
            or not 1 <= final_service_max_failures <= 999_999
        ):
            raise ValueError(
                "final_service_max_failures must be an integer from 1 to 999999"
            )
        self.final_service_max_failures = final_service_max_failures
        self.state = "new"
        self.release: ReleaseInfo | None = None

    def _run(
        self,
        argv: Sequence[str],
        *,
        timeout: int = COMMAND_TIMEOUT_SECONDS,
        journal: bool = False,
    ) -> CommandResult:
        return self.runner.run(argv, timeout=timeout, journal=journal)

    def _owner_ok(self, metadata: os.stat_result) -> bool:
        if self.expected_uid is not None and metadata.st_uid != self.expected_uid:
            return False
        if self.expected_gid is not None and metadata.st_gid != self.expected_gid:
            return False
        return True

    def _read_regular(
        self,
        path: Path,
        *,
        expected_mode: int,
        maximum: int,
        code: str,
    ) -> str:
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise ControllerFault(code, f"required file is missing: {path}") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or _mode(metadata) != expected_mode
            or not self._owner_ok(metadata)
            or metadata.st_size > maximum
        ):
            raise ControllerFault(code, f"unsafe file metadata: {path}")
        try:
            payload = path.read_bytes()
            return payload.decode("ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise ControllerFault(code, f"cannot read canonical ASCII file: {path}") from exc

    def _current_boot_id(self) -> str:
        if self.boot_id_override is not None:
            return validate_boot_id(self.boot_id_override, "current boot ID")
        text = self._read_regular(
            self.paths.boot_id,
            expected_mode=0o444,
            maximum=64,
            code="invalid-state",
        )
        try:
            return validate_boot_id(text.strip(), "current boot ID")
        except StateFormatError as exc:
            raise ControllerFault("invalid-state", str(exc)) from exc

    def _read_checkpoint(self) -> str | None:
        path = self.paths.checkpoint
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or _mode(metadata) != 0o600
            or not self._owner_ok(metadata)
            or metadata.st_size > 64
        ):
            raise CheckpointFault(
                "checkpoint-corrupt", "setup checkpoint has unsafe metadata"
            )
        try:
            text = path.read_bytes().decode("ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise CheckpointFault(
                "checkpoint-corrupt", "setup checkpoint is unreadable"
            ) from exc
        if text.count("\n") != 1 or not text.endswith("\n"):
            raise CheckpointFault(
                "checkpoint-corrupt", "setup checkpoint is not one canonical line"
            )
        try:
            return validate_boot_id(text[:-1], "setup checkpoint")
        except StateFormatError as exc:
            raise CheckpointFault("checkpoint-corrupt", str(exc)) from exc

    def _verify_release(self) -> ReleaseInfo:
        if self.release_override is not None:
            return self.release_override

        metadata_text = self._read_regular(
            self.paths.metadata,
            expected_mode=0o644,
            maximum=4096,
            code="release-integrity",
        )
        fields: dict[str, str] = {}
        for line in metadata_text.splitlines():
            key, separator, value = line.partition("=")
            if separator != "=" or not key or not value or key in fields:
                raise ControllerFault(
                    "release-integrity", "invalid image metadata"
                )
            fields[key] = value
        try:
            release = ReleaseInfo(
                fields["release_name"], fields["release_setup_sha256"]
            )
        except (KeyError, StateFormatError) as exc:
            raise ControllerFault(
                "release-integrity", "missing or invalid release metadata"
            ) from exc

        expected_link = f"releases/{release.name}"
        current = self.paths.path("/opt/pi-router/current")
        try:
            if not current.is_symlink() or os.readlink(current) != expected_link:
                raise ControllerFault(
                    "release-integrity", "stable release symlink mismatch"
                )
        except OSError as exc:
            raise ControllerFault(
                "release-integrity", "cannot inspect stable release symlink"
            ) from exc

        setup = self.paths.path(f"/opt/pi-router/releases/{release.name}/setup")
        try:
            metadata = setup.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or not self._owner_ok(metadata)
                or not (_mode(metadata) & 0o111)
            ):
                raise ControllerFault(
                    "release-integrity", "unsafe versioned setup executable"
                )
            actual = hashlib.sha256(setup.read_bytes()).hexdigest()
        except OSError as exc:
            raise ControllerFault(
                "release-integrity", "cannot hash versioned setup"
            ) from exc
        if actual != release.setup_sha256:
            raise ControllerFault(
                "release-integrity", "versioned setup checksum mismatch"
            )
        return release

    def _attempt(
        self,
        operation: str,
        boot_id: str,
        checkpoint_before: str,
        *,
        late_apply_before: str,
        original_pre_boot_id: str,
    ) -> Attempt:
        assert self.release is not None
        attempt = Attempt(
            operation=operation,
            invocation_boot_id=boot_id,
            checkpoint_before=checkpoint_before,
            release_name=self.release.name,
            release_setup_sha256=self.release.setup_sha256,
            late_apply_before=late_apply_before,
            original_pre_boot_id=original_pre_boot_id,
        )
        self.store.write_attempt(attempt)
        return attempt

    def _validate_attempt_release(self, attempt: Attempt) -> None:
        assert self.release is not None
        if (
            attempt.release_name != self.release.name
            or attempt.release_setup_sha256 != self.release.setup_sha256
        ):
            raise ControllerFault(
                "release-integrity", "attempt belongs to another release"
            )

    def _write_status(self, code: str) -> None:
        self.store.write_status(self.state, code)

    def _best_effort(self, argv: Sequence[str]) -> CommandResult:
        return self._run(argv, timeout=COMMAND_TIMEOUT_SECONDS, journal=False)

    def _stop_onboarding(self) -> bool:
        result = self._best_effort(
            (
                self.SYSTEMCTL,
                "stop",
                "pi-router-onboarding-server.service",
                "pi-router-onboarding-backend.service",
            )
        )
        if result.returncode == 0:
            return True

        # A failed multi-unit stop can still leave both units inactive (for
        # example, if one was already stopped).  Only treat that as success
        # when systemd explicitly confirms a non-running ActiveState for each
        # unit. Unknown or empty results retain the provisioner timer so the
        # cleanup is retried.
        for unit in (
            "pi-router-onboarding-server.service",
            "pi-router-onboarding-backend.service",
        ):
            state = self._best_effort((self.SYSTEMCTL, "is-active", unit))
            if state.output.strip() not in {"inactive", "failed"}:
                return False
        return True

    def _disable_timer(self) -> None:
        self._best_effort(
            (
                self.SYSTEMCTL,
                "disable",
                "--now",
                "pi-router-image-controller.timer",
            )
        )

    def _request_reboot(self, status: str) -> str:
        self._write_status(status)
        result = self._best_effort((self.SYSTEMCTL, "--no-block", "reboot"))
        return "reboot-requested" if result.returncode == 0 else "reboot-pending"

    def _terminal(self, code: str) -> str:
        self.store.write_error(self.state, code)
        self.store.write_status(self.state, "error")
        if not self._stop_onboarding():
            return "terminal-cleanup-pending"
        self._disable_timer()
        return "terminal"

    def _ensure_identity(self) -> None:
        self._write_status("initializing-identity")
        path = self.paths.machine_id
        try:
            metadata = path.lstat()
            text = path.read_text(encoding="ascii").strip()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            metadata = None
            text = ""

        if text in {"", "uninitialized"}:
            result = self._run((self.MACHINE_ID_SETUP, "--commit"))
            if result.returncode != 0:
                raise ControllerFault(
                    "identity-generation-failed", "machine ID setup failed"
                )
            try:
                metadata = path.lstat()
                text = path.read_text(encoding="ascii").strip()
            except (OSError, UnicodeDecodeError) as exc:
                raise ControllerFault(
                    "identity-generation-failed", "machine ID is unavailable"
                ) from exc

        if (
            metadata is None
            or not stat.S_ISREG(metadata.st_mode)
            or not self._owner_ok(metadata)
            or MACHINE_ID_RE.fullmatch(text) is None
            or int(text, 16) == 0
        ):
            raise ControllerFault(
                "identity-generation-failed", "machine ID is not target-unique"
            )

        private_keys = [
            path
            for path in self.paths.ssh_dir.glob("ssh_host_*_key")
            if path.is_file() and not path.is_symlink() and path.stat().st_size > 0
        ]
        if not private_keys:
            result = self._run((self.SSH_KEYGEN, "-A"))
            if result.returncode != 0:
                raise ControllerFault(
                    "identity-generation-failed", "SSH host-key generation failed"
                )
            private_keys = [
                path
                for path in self.paths.ssh_dir.glob("ssh_host_*_key")
                if path.is_file() and not path.is_symlink() and path.stat().st_size > 0
            ]
        if not private_keys:
            raise ControllerFault(
                "identity-generation-failed", "no target SSH host key exists"
            )

    def _ensure_admin_account(self) -> None:
        """Require the account that Raspberry Pi Imager was told to create."""

        passwd = self._run((self.GETENT, "passwd", "admin"))
        group = self._run((self.GETENT, "group", "admin"))
        password = self._run((self.PASSWD, "--status", "admin"))
        if passwd.returncode != 0 or group.returncode != 0 or password.returncode != 0:
            raise ControllerFault(
                "os-customization-failed", "Raspberry Pi Imager did not create admin"
            )

        passwd_fields = passwd.output.strip().split(":")
        group_fields = group.output.strip().split(":")
        password_fields = password.output.strip().split()
        if (
            len(passwd_fields) != 7
            or passwd_fields[0] != "admin"
            or passwd_fields[2:4] != ["1000", "1000"]
            or passwd_fields[5] != "/home/admin"
            or passwd_fields[6] != "/bin/bash"
            or len(group_fields) != 4
            or group_fields[:3] != ["admin", "x", "1000"]
            or len(password_fields) < 2
            or password_fields[:2] != ["admin", "P"]
        ):
            raise ControllerFault(
                "os-customization-failed", "admin does not match the Imager contract"
            )

        try:
            home_metadata = self.paths.admin_home.lstat()
        except OSError as exc:
            raise ControllerFault(
                "os-customization-failed", "admin home is unavailable"
            ) from exc
        if not stat.S_ISDIR(home_metadata.st_mode) or self.paths.admin_home.is_symlink():
            raise ControllerFault(
                "os-customization-failed", "admin home is unsafe"
            )

    def _parse_router_config(self) -> Mapping[str, str]:
        text = self._read_regular(
            self.paths.router_config,
            expected_mode=0o644,
            maximum=8192,
            code="invalid-config",
        )
        fields: dict[str, str] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = ASSIGNMENT_RE.fullmatch(line)
            if match is None or match.group(1) not in CONFIG_KEYS:
                raise ControllerFault("invalid-config", "invalid router assignment")
            key, value = match.groups()
            if key in fields:
                raise ControllerFault("invalid-config", "duplicate router assignment")
            fields[key] = value
        if set(fields) != set(CONFIG_KEYS):
            raise ControllerFault("invalid-config", "incomplete router configuration")
        if fields["ROUTER_USER"] != "admin":
            raise ControllerFault("invalid-config", "unexpected router account")
        if (
            INTERFACE_RE.fullmatch(fields["LAN_IF"]) is None
            or INTERFACE_RE.fullmatch(fields["WAN_IF"]) is None
            or fields["LAN_IF"] == fields["WAN_IF"]
        ):
            raise ControllerFault("invalid-config", "invalid detected interfaces")
        try:
            lan = ipaddress.IPv4Interface(
                f'{fields["LAN_ADDR"]}/{fields["LAN_PREFIX"]}'
            )
            start = ipaddress.IPv4Address(fields["DHCP_START"])
            end = ipaddress.IPv4Address(fields["DHCP_END"])
            ipaddress.IPv4Address(fields["DNS_UPSTREAM"])
        except ValueError as exc:
            raise ControllerFault("invalid-config", "invalid router addressing") from exc
        if start not in lan.network or end not in lan.network or int(start) > int(end):
            raise ControllerFault("invalid-config", "DHCP range is outside the LAN")
        return fields

    def _config_destination_kind(self) -> str:
        """Classify the setup-owned config without following a final symlink."""

        try:
            metadata = self.paths.router_config.lstat()
        except FileNotFoundError:
            return "absent"
        except OSError as exc:
            raise ControllerFault("invalid-config", "cannot inspect router config") from exc
        if (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
            and _mode(metadata) == 0o644
            and self._owner_ok(metadata)
            and metadata.st_size <= 8192
        ):
            return "safe-regular"
        return "unsafe"

    def _ensure_config(
        self,
        boot_id: str,
        *,
        recovering: bool = False,
    ) -> Mapping[str, str]:
        kind = self._config_destination_kind()
        if kind == "unsafe":
            raise ControllerFault("invalid-config", "unsafe router config destination")
        if kind == "safe-regular":
            try:
                config = self._parse_router_config()
            except ControllerFault:
                if not recovering:
                    raise
            else:
                if recovering:
                    self.store.clear_attempt()
                return config

        self._write_status("detecting-hardware")
        self._attempt(
            "config",
            boot_id,
            "absent",
            late_apply_before="absent",
            original_pre_boot_id="absent",
        )
        result = self._run(
            (
                str(self.paths.setup),
                "--live",
                "--allow-live-install",
                "--phase",
                "config",
                "--answers",
                str(self.paths.image_answers),
                "--non-interactive",
            ),
            timeout=SETUP_TIMEOUT_SECONDS,
            journal=True,
        )
        if result.returncode != 0:
            raise ControllerFault("hardware-mismatch", "setup config failed")
        config = self._parse_router_config()
        self.store.clear_attempt()
        return config

    def _carrier(self, interface: str) -> bool:
        path = self.paths.path(f"/sys/class/net/{interface}/carrier")
        try:
            return path.read_text(encoding="ascii").strip() == "1"
        except (OSError, UnicodeDecodeError):
            return False

    def _gate_carriers(self, config: Mapping[str, str]) -> bool:
        for interface in (config["LAN_IF"], config["WAN_IF"]):
            self._run((self.IP, "link", "set", "dev", interface, "up"))
        if not self._carrier(config["LAN_IF"]):
            self._write_status("waiting-lan-carrier")
            return False
        if not self._carrier(config["WAN_IF"]):
            self._write_status("waiting-wan-carrier")
            return False
        return True

    def _wan_route(self, wan_if: str) -> bool:
        result = self._run(
            (self.IP, "-4", "route", "show", "default", "dev", wan_if)
        )
        return result.returncode == 0 and bool(result.output.strip())

    def _ensure_bootstrap_wan(self, wan_if: str) -> bool:
        if not self._wan_route(wan_if):
            show = self._run(
                (self.NMCLI, "-g", "connection.uuid", "connection", "show", "id", BOOTSTRAP_CONNECTION)
            )
            if show.returncode != 0:
                add = self._run(
                    (
                        self.NMCLI,
                        "connection",
                        "add",
                        "type",
                        "ethernet",
                        "con-name",
                        BOOTSTRAP_CONNECTION,
                        "ifname",
                        wan_if,
                        "connection.autoconnect",
                        "yes",
                        "connection.autoconnect-priority",
                        "999",
                        "ipv4.method",
                        "auto",
                        "ipv4.ignore-auto-dns",
                        "no",
                        "ipv4.never-default",
                        "no",
                        "ipv6.method",
                        "disabled",
                    )
                )
                if add.returncode != 0:
                    self._write_status("waiting-wan-dhcp")
                    return False
            up = self._run(
                (
                    self.NMCLI,
                    "connection",
                    "up",
                    "id",
                    BOOTSTRAP_CONNECTION,
                    "ifname",
                    wan_if,
                )
            )
            if up.returncode != 0:
                self._write_status("waiting-wan-dhcp")
                return False
        if not self._wan_route(wan_if):
            self._write_status("waiting-wan-dhcp")
            return False
        return True

    def _repository_target(self) -> tuple[str, str]:
        assert self.release is not None
        path = self.paths.path(
            f"/opt/pi-router/releases/{self.release.name}/"
            "config/proton-repository.conf"
        )
        text = self._read_regular(
            path,
            expected_mode=0o644,
            maximum=4096,
            code="release-integrity",
        )
        values: dict[str, list[str]] = {
            key: [] for key in PROTON_REPOSITORY_KEYS
        }
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = ASSIGNMENT_RE.fullmatch(line)
            if match is None:
                raise ControllerFault(
                    "release-integrity", "invalid repository metadata"
                )
            key, value = match.groups()
            if key in values:
                values[key].append(value)
        if any(len(values[key]) != 1 for key in PROTON_REPOSITORY_KEYS):
            raise ControllerFault(
                "release-integrity", "incomplete or duplicate repository metadata"
            )
        host = values["PROTON_REPO_HOST"][0]
        path = values["PROTON_REPO_PATH"][0]
        package = values["PROTON_REPO_PACKAGE"][0]
        if DNS_HOSTNAME_RE.fullmatch(host) is None:
            raise ControllerFault(
                "release-integrity", "invalid Proton repository host"
            )
        if (
            REPOSITORY_PATH_RE.fullmatch(path) is None
            or any(segment in {".", ".."} for segment in path.split("/"))
        ):
            raise ControllerFault(
                "release-integrity", "invalid Proton repository path"
            )
        if REPOSITORY_PACKAGE_RE.fullmatch(package) is None:
            raise ControllerFault(
                "release-integrity", "invalid Proton repository package"
            )
        return host, f"https://{host}{path}/{package}"

    def _repository_reachable(self, wan_if: str) -> bool:
        host, artifact_url = self._repository_target()
        lookup = self._run((self.GETENT, "ahostsv4", host))
        if lookup.returncode != 0:
            self._write_status("waiting-repository")
            return False
        address = ""
        for line in lookup.output.splitlines():
            candidate = line.split(maxsplit=1)[0] if line.split() else ""
            try:
                ipaddress.IPv4Address(candidate)
            except ValueError:
                continue
            address = candidate
            break
        if not address:
            self._write_status("waiting-repository")
            return False
        route = self._run((self.IP, "-4", "route", "get", address))
        if (
            route.returncode != 0
            or re.search(rf"(?:^|\s)dev\s+{re.escape(wan_if)}(?:\s|$)", route.output)
            is None
        ):
            self._write_status("waiting-repository")
            return False
        probe = self._run(
            (
                self.WGET,
                "--https-only",
                "--spider",
                "--timeout=15",
                "--tries=1",
                artifact_url,
            ),
            timeout=30,
        )
        if probe.returncode != 0:
            self._write_status("waiting-repository")
            return False
        return True

    def _indicator_kind(self, config: Mapping[str, str]) -> str | None:
        path = self.paths.provisioning_indicator
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or _mode(metadata) != 0o644
            or not self._owner_ok(metadata)
        ):
            raise ControllerFault(
                "resume-unsafe", "unsafe provisioning indicator"
            )
        try:
            actual = path.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise ControllerFault(
                "resume-unsafe", "unreadable provisioning indicator"
            ) from exc
        full = "\n".join(
            (
                "# Generated temporarily by pi-router setup.",
                "# Give provisioning clients a LAN address only.",
                "# Do not advertise this Pi as router or DNS until Proton is ready.",
                f'interface={config["LAN_IF"]}',
                f'dhcp-range={config["DHCP_START"]},{config["DHCP_END"]},{config["DHCP_LEASE"]}',
                f'server={config["DNS_UPSTREAM"]}',
                "dhcp-option=3",
                "dhcp-option=6",
                "",
            )
        )
        reduced = "\n".join(
            (
                "# Generated temporarily by pi-router setup.",
                "# Permanent DHCP range is now owned by generated router configuration.",
                "# Keep gateway/DNS suppressed until Proton is ready.",
                "# Keep a temporary upstream so the Pi itself retains DNS.",
                f'server={config["DNS_UPSTREAM"]}',
                "dhcp-option=3",
                "dhcp-option=6",
                "",
            )
        )
        if actual == full:
            return "full"
        if actual == reduced:
            return "reduced"
        raise ControllerFault(
            "resume-unsafe",
            "provisioning indicator does not exactly match setup output",
        )

    def _resume_prerequisites(self, config: Mapping[str, str]) -> bool:
        lan = self._run(
            (self.NMCLI, "-g", "GENERAL.CONNECTION", "device", "show", config["LAN_IF"])
        )
        wan = self._run(
            (self.NMCLI, "-g", "GENERAL.CONNECTION", "device", "show", config["WAN_IF"])
        )
        lan_addr = self._run(
            (self.IP, "-4", "-o", "addr", "show", "dev", config["LAN_IF"])
        )
        expected_address = f'{config["LAN_ADDR"]}/{config["LAN_PREFIX"]}'
        return (
            lan.returncode == 0
            and lan.output.strip() == "LAN"
            and wan.returncode == 0
            and wan.output.strip() == "WAN"
            and lan_addr.returncode == 0
            and expected_address in lan_addr.output.split()
            and self._wan_route(config["WAN_IF"])
        )

    def _setup_command(self, phase: str) -> tuple[str, ...]:
        common = (str(self.paths.setup), "--live", "--allow-live-install", "--phase", phase)
        if phase in {"pre-reboot", "pre-reboot-resume"}:
            return common + (
                "--answers",
                str(self.paths.router_config),
                "--allow-network-cutover",
                "--allow-wifi-disable",
                "--non-interactive",
            )
        if phase == "final-verify":
            return common + ("--non-interactive",)
        raise ValueError(f"unsupported controller setup phase: {phase}")

    def _pre_output_valid(self, result: CommandResult) -> bool:
        return result.returncode == 0 and all(
            marker in result.output for marker in PRE_MARKERS
        )

    def _run_pre_full(self, boot_id: str) -> str:
        self._write_status("running-pre-reboot")
        for attempt_number in range(2):
            self._attempt(
                "pre-full",
                boot_id,
                "absent",
                late_apply_before="absent",
                original_pre_boot_id="absent",
            )
            result = self._run(
                self._setup_command("pre-reboot"),
                timeout=SETUP_TIMEOUT_SECONDS,
                journal=True,
            )
            checkpoint = self._read_checkpoint()
            if checkpoint is not None:
                if checkpoint != boot_id:
                    raise ControllerFault(
                        "checkpoint-conflict", "pre-reboot wrote another boot ID"
                    )
                # The checkpoint is setup's documented success-only commit
                # point. It remains authoritative when status/output is lost.
                if not self._pre_output_valid(result):
                    print(
                        "pi-router-image-controller: pre-reboot output was incomplete; "
                        "reconciled from setup checkpoint",
                        file=sys.stderr,
                    )
                return self._commit_pre(boot_id)

            config = self._parse_router_config()
            indicator = self._indicator_kind(config)
            if indicator is not None:
                return self._run_pre_resume(boot_id, boot_id, config)
            if attempt_number == 0:
                continue
            raise ControllerFault(
                "setup-pre-failed", "pre-reboot failed before its commit point"
            )
        raise AssertionError("unreachable pre-reboot retry exit")

    def _run_pre_resume(
        self,
        boot_id: str,
        original_pre_boot_id: str,
        config: Mapping[str, str],
    ) -> str:
        if self._indicator_kind(config) is None or not self._resume_prerequisites(config):
            raise ControllerFault(
                "resume-unsafe", "resume prerequisites are not proven"
            )
        self._write_status("resuming-pre-reboot")
        for attempt_number in range(2):
            self._attempt(
                "pre-resume",
                boot_id,
                "absent",
                late_apply_before="present",
                original_pre_boot_id=original_pre_boot_id,
            )
            result = self._run(
                self._setup_command("pre-reboot-resume"),
                timeout=SETUP_TIMEOUT_SECONDS,
                journal=True,
            )
            checkpoint = self._read_checkpoint()
            if checkpoint is not None:
                if checkpoint != boot_id:
                    raise ControllerFault(
                        "checkpoint-conflict", "pre-resume wrote another boot ID"
                    )
                if not self._pre_output_valid(result):
                    print(
                        "pi-router-image-controller: pre-resume output was incomplete; "
                        "reconciled from setup checkpoint",
                        file=sys.stderr,
                    )
                return self._commit_pre(boot_id)
            if self._indicator_kind(config) is None:
                raise ControllerFault(
                    "resume-unsafe", "resume proof disappeared"
                )
            if attempt_number == 1:
                raise ControllerFault(
                    "setup-pre-failed", "pre-reboot resume did not commit"
                )
        raise AssertionError("unreachable pre-resume retry exit")

    def _verify_firewall(self) -> None:
        result = self._run((self.NFT, "list", "table", "inet", "router_filter"))
        if (
            result.returncode != 0
            or "chain forward" not in result.output
            or "policy drop" not in result.output
            or "chain host_wan_bootstrap" not in result.output
        ):
            raise ControllerFault(
                "setup-pre-failed", "canonical fail-closed table is absent"
            )

    def _activate_imager_ssh(self) -> None:
        """Start SSH only if Imager selected it, after LAN/firewall cutover."""

        enabled = self._run((self.SYSTEMCTL, "is-enabled", "ssh.service"))
        state = enabled.output.strip()
        if enabled.returncode != 0:
            if state in {"disabled", "masked", "not-found"}:
                return
            raise ControllerFault(
                "setup-pre-failed", "cannot determine the Imager SSH selection"
            )
        if state not in {"enabled", "enabled-runtime"}:
            raise ControllerFault(
                "setup-pre-failed", "unexpected Imager SSH enablement state"
            )
        restarted = self._run((self.SYSTEMCTL, "restart", "ssh.service"))
        active = self._run((self.SYSTEMCTL, "is-active", "--quiet", "ssh.service"))
        if restarted.returncode != 0 or active.returncode != 0:
            raise ControllerFault(
                "setup-pre-failed", "selected LAN-only SSH service did not start"
            )

    def _durable_unlink(
        self,
        path: Path,
        *,
        expected_mode: int,
        expected_content: str,
        code: str,
        label: str,
    ) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or _mode(metadata) != expected_mode
            or not self._owner_ok(metadata)
        ):
            raise ControllerFault(code, f"unsafe {label}")
        try:
            if path.read_text(encoding="ascii") != expected_content:
                raise ControllerFault(code, f"{label} content changed")
            path.unlink()
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                try:
                    os.fsync(descriptor)
                except OSError as exc:
                    if os.name != "nt" or exc.errno not in {errno.EINVAL, errno.EBADF}:
                        raise
            finally:
                os.close(descriptor)
        except ControllerFault:
            raise
        except (OSError, UnicodeError) as exc:
            raise ControllerFault(code, f"cannot durably remove {label}") from exc

    def _remove_bootstrap_profile(self) -> None:
        show = self._run(
            (self.NMCLI, "-g", "connection.uuid", "connection", "show", "id", BOOTSTRAP_CONNECTION)
        )
        if show.returncode == 0:
            deleted = self._run(
                (self.NMCLI, "connection", "delete", "id", BOOTSTRAP_CONNECTION)
            )
            if deleted.returncode != 0:
                raise ControllerFault(
                    "setup-pre-failed", "temporary WAN profile was not removed"
                )

    def _commit_pre(self, committed_boot_id: str) -> str:
        checkpoint = self._read_checkpoint()
        if checkpoint != committed_boot_id:
            raise ControllerFault(
                "checkpoint-conflict", "pre-reboot checkpoint proof changed"
            )
        self._verify_firewall()
        self._activate_imager_ssh()
        self._remove_bootstrap_profile()
        self._durable_unlink(
            self.paths.forwarding_guard,
            expected_mode=0o644,
            expected_content=(
                "# Temporary image-build guard. The provisioner removes this only after setup's\n"
                "# pre-reboot checkpoint proves the canonical fail-closed firewall was applied.\n"
                "net.ipv4.ip_forward=0\n"
            ),
            code="forwarding-guard",
            label="forwarding guard",
        )
        applied = self._run((self.SYSCTL, "--system"), journal=True)
        forwarding = self._run((self.SYSCTL, "-n", "net.ipv4.ip_forward"))
        if (
            applied.returncode != 0
            or forwarding.returncode != 0
            or forwarding.output.strip() != "1"
        ):
            raise ControllerFault(
                "forwarding-guard", "IPv4 forwarding did not transition to one"
            )
        self._durable_unlink(
            self.paths.package_service_guard,
            expected_mode=0o755,
            expected_content=(
                "#!/bin/sh\n"
                "# Temporary image-owned guard: package installation during provisioning\n"
                "# pre-reboot phase must not start services before setup applies router policy.\n"
                "exit 101\n"
            ),
            code="setup-pre-failed",
            label="package service guard",
        )
        self.store.write_state("pre-reboot-complete")
        self.state = "pre-reboot-complete"
        self.store.clear_attempt()
        current = self._current_boot_id()
        if checkpoint == current:
            return self._request_reboot("rebooting-after-pre")
        return self._start_onboarding()

    def _start_onboarding(self) -> str:
        result = self._run((self.SYSTEMCTL, "start", "pi-router-onboarding-server.service"))
        status = "waiting-onboarding" if result.returncode == 0 else "waiting-desktop"
        self._write_status(status)
        return status

    def _commit_post(self, checkpoint: str, current_boot: str) -> str:
        self.store.clear_final_wait()
        self.store.write_state("post-reboot-complete")
        self.state = "post-reboot-complete"
        self.store.clear_attempt()
        self._stop_onboarding()
        if checkpoint == current_boot:
            return self._request_reboot("rebooting-after-post")
        return "post-reboot-complete"

    def _final_services_ready(self, config: Mapping[str, str]) -> bool:
        for unit in (
            "NetworkManager.service",
            "nftables.service",
            "dnsmasq.service",
            "lightdm.service",
        ):
            if self._run(
                (self.SYSTEMCTL, "is-active", "--quiet", unit)
            ).returncode != 0:
                return False
        wan = self._run(
            (self.NMCLI, "-g", "GENERAL.CONNECTION", "device", "show", config["WAN_IF"])
        )
        proton = self._run(
            (self.NMCLI, "-g", "GENERAL.TYPE", "device", "show", "proton0")
        )
        return (
            wan.returncode == 0
            and wan.output.strip() == "WAN"
            and self._wan_route(config["WAN_IF"])
            and proton.returncode == 0
            and proton.output.strip() == "wireguard"
        )

    def _begin_final_service_check(self, boot_id: str) -> int:
        wait = self.store.read_final_wait()
        if wait is None or wait.boot_id != boot_id:
            checks_started = 1
        else:
            checks_started = wait.checks_started + 1
        self.store.write_final_wait(FinalServiceWait(boot_id, checks_started))
        return checks_started

    def _commit_ready(self, boot_id: str, proof: str) -> str:
        assert self.release is not None
        self.store.write_ready(self.release.name, boot_id, proof)
        self.store.write_state("ready")
        self.state = "ready"
        self.store.clear_attempt()
        self.store.clear_final_wait()
        self.store.clear_error()
        self._write_status("ready")
        if not self._stop_onboarding():
            return "ready-cleanup-pending"
        self._disable_timer()
        return "ready"

    def _run_final(self, boot_id: str, checkpoint: str) -> str:
        if checkpoint == boot_id:
            self.store.clear_final_wait()
            return self._request_reboot("rebooting-after-post")
        config = self._parse_router_config()
        wait = self.store.read_final_wait()
        if (
            wait is not None
            and wait.boot_id == boot_id
            and wait.checks_started >= self.final_service_max_failures
        ):
            raise ControllerFault(
                "final-services-timeout",
                "router services exhausted the bounded settling period",
            )
        checks_started = self._begin_final_service_check(boot_id)
        if not self._final_services_ready(config):
            if checks_started >= self.final_service_max_failures:
                raise ControllerFault(
                    "final-services-timeout",
                    "router services did not become ready within the bounded settling period",
                )
            self._write_status("waiting-final-services")
            return "waiting-final-services"
        self.store.clear_final_wait()
        self._write_status("running-final-verify")
        self._attempt(
            "final",
            boot_id,
            checkpoint,
            late_apply_before="not-applicable",
            original_pre_boot_id="not-applicable",
        )
        result = self._run(
            self._setup_command("final-verify"),
            timeout=SETUP_TIMEOUT_SECONDS,
            journal=True,
        )
        after = self._read_checkpoint()
        if after is None:
            if result.returncode == 0 and FINAL_MARKER in result.output:
                return self._commit_ready(boot_id, "exit-zero-and-marker")
            # Checkpoint consumption is setup's success-only commit point, but
            # the current invocation did not provide the normal exit/marker
            # proof.  Preserve durable intent and reconcile on the next pass;
            # never classify the current, observed failure as normal success.
            self._write_status("waiting-final-recovery")
            return "waiting-final-recovery"
        if after != checkpoint:
            raise ControllerFault(
                "checkpoint-conflict", "final verification changed checkpoint"
            )
        raise ControllerFault(
            "setup-final-failed", "final verification did not consume checkpoint"
        )

    def _handle_new(
        self,
        boot_id: str,
        checkpoint: str | None,
        attempt: Attempt | None,
    ) -> str:
        if attempt is not None:
            if attempt.operation == "config":
                if checkpoint is not None:
                    raise ControllerFault(
                        "checkpoint-conflict",
                        "config attempt conflicts with setup checkpoint",
                    )
                self._ensure_identity()
                config = self._ensure_config(boot_id, recovering=True)
                if not self._gate_carriers(config):
                    return "waiting-carrier"
                if not self._ensure_bootstrap_wan(config["WAN_IF"]):
                    return "waiting-wan-dhcp"
                if not self._repository_reachable(config["WAN_IF"]):
                    return "waiting-repository"
                return self._run_pre_full(boot_id)
            if attempt.operation not in {"pre-full", "pre-resume"}:
                raise ControllerFault(
                    "checkpoint-conflict", "attempt is incompatible with new state"
                )
            if checkpoint is not None:
                if checkpoint != attempt.invocation_boot_id:
                    raise ControllerFault(
                        "checkpoint-conflict", "pre attempt checkpoint mismatch"
                    )
                return self._commit_pre(checkpoint)
            config = self._parse_router_config()
            if self._indicator_kind(config) is None:
                raise ControllerFault(
                    "resume-unsafe", "interrupted pre phase has no resume proof"
                )
            original = (
                attempt.invocation_boot_id
                if attempt.operation == "pre-full"
                else attempt.original_pre_boot_id
            )
            return self._run_pre_resume(boot_id, original, config)

        if checkpoint is not None:
            raise ControllerFault(
                "checkpoint-conflict", "checkpoint has no durable pre attempt"
            )

        self._ensure_identity()
        config = self._ensure_config(boot_id)
        if not self._gate_carriers(config):
            return "waiting-carrier"
        if not self._ensure_bootstrap_wan(config["WAN_IF"]):
            return "waiting-wan-dhcp"
        if not self._repository_reachable(config["WAN_IF"]):
            return "waiting-repository"
        return self._run_pre_full(boot_id)

    def _handle_pre(
        self,
        boot_id: str,
        checkpoint: str | None,
        attempt: Attempt | None,
    ) -> str:
        if checkpoint is None:
            raise ControllerFault(
                "checkpoint-missing", "pre-reboot-complete requires a checkpoint"
            )
        if attempt is not None:
            if attempt.operation in {"pre-full", "pre-resume"}:
                if checkpoint != attempt.invocation_boot_id:
                    raise ControllerFault(
                        "checkpoint-conflict", "stale pre attempt conflicts"
                    )
                self.store.clear_attempt()
            elif attempt.operation == "post":
                if checkpoint == attempt.invocation_boot_id:
                    return self._commit_post(checkpoint, boot_id)
                if checkpoint == attempt.checkpoint_before:
                    self.store.clear_attempt()
                else:
                    raise ControllerFault(
                        "checkpoint-conflict", "post attempt checkpoint mismatch"
                    )
            else:
                raise ControllerFault(
                    "checkpoint-conflict", "final attempt exists in pre state"
                )
        if checkpoint == boot_id:
            return self._request_reboot("rebooting-after-pre")
        return self._start_onboarding()

    def _handle_post(
        self,
        boot_id: str,
        checkpoint: str | None,
        attempt: Attempt | None,
    ) -> str:
        self._stop_onboarding()
        if attempt is not None:
            if attempt.operation == "post":
                if checkpoint != attempt.invocation_boot_id:
                    raise ControllerFault(
                        "checkpoint-conflict", "committed post attempt conflicts"
                    )
                self.store.clear_attempt()
            elif attempt.operation == "final":
                if checkpoint is None:
                    return self._commit_ready(
                        attempt.invocation_boot_id,
                        "checkpoint-consumed-after-durable-intent",
                    )
                if checkpoint != attempt.checkpoint_before:
                    raise ControllerFault(
                        "checkpoint-conflict", "final attempt checkpoint mismatch"
                    )
                return self._run_final(boot_id, checkpoint)
            else:
                raise ControllerFault(
                    "checkpoint-conflict", "pre attempt exists in post state"
                )
        if checkpoint is None:
            raise ControllerFault(
                "checkpoint-missing", "post-reboot-complete requires a checkpoint"
            )
        return self._run_final(boot_id, checkpoint)

    def _handle_ready(
        self,
        checkpoint: str | None,
        attempt: Attempt | None,
    ) -> str:
        if checkpoint is not None:
            raise ControllerFault(
                "checkpoint-conflict", "ready state retains setup checkpoint"
            )
        if attempt is not None:
            if attempt.operation != "final":
                raise ControllerFault(
                    "invalid-state", "ready state retains incompatible attempt"
                )
            self.store.clear_attempt()
        receipt = self.store.read_ready()
        assert self.release is not None
        if receipt is None or receipt.release_name != self.release.name:
            raise ControllerFault(
                "invalid-state", "ready state has no matching receipt"
            )
        self._write_status("ready")
        self.store.clear_final_wait()
        if not self._stop_onboarding():
            return "ready-cleanup-pending"
        self._disable_timer()
        return "ready"

    def run_once(self) -> str:
        """Run one serialized state-machine pass.

        Expected operational failures are converted to durable terminal
        receipts.  State-store durability/safety failures intentionally escape:
        if the receipt store itself cannot be trusted, fabricating an error
        receipt would only conceal that condition.
        """

        self.store.ensure_directory()
        self.state = self.store.read_state()
        if self.store.read_error() is not None:
            if not self._stop_onboarding():
                return "terminal-cleanup-pending"
            self._disable_timer()
            return "terminal"

        try:
            self._ensure_admin_account()
            self.release = self._verify_release()
            boot_id = self._current_boot_id()
            checkpoint = self._read_checkpoint()
            attempt = self.store.read_attempt()
            if attempt is not None:
                self._validate_attempt_release(attempt)

            if self.state == "new":
                return self._handle_new(boot_id, checkpoint, attempt)
            if self.state == "pre-reboot-complete":
                return self._handle_pre(boot_id, checkpoint, attempt)
            if self.state == "post-reboot-complete":
                return self._handle_post(boot_id, checkpoint, attempt)
            if self.state == "ready":
                return self._handle_ready(checkpoint, attempt)
            raise ControllerFault("invalid-state", "unknown controller state")
        except CheckpointFault as exc:
            return self._terminal(exc.code)
        except ControllerFault as exc:
            return self._terminal(exc.code)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        if fcntl is None:  # pragma: no cover - production is Linux.
            raise RuntimeError("provisioner lock requires POSIX fcntl")
        self.paths.lock.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.paths.lock, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockUnavailable from exc
            yield
        finally:
            os.close(descriptor)

    def run_locked(self) -> str:
        try:
            with self.lock():
                return self.run_once()
        except LockUnavailable:
            return "busy"


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print(
            "pi-router-image-controller: no command-line options are supported",
            file=sys.stderr,
        )
        return 2
    controller = ProvisioningController()
    try:
        outcome = controller.run_locked()
    except ImageStateError as exc:
        print(
            f"pi-router-image-controller: unsafe image state: {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # Preserve diagnostics for unexpected code faults.
        print(
            f"pi-router-image-controller: internal failure: {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"pi-router-image-controller: {outcome}")
    return 1 if outcome == "terminal" else 0


if __name__ == "__main__":
    raise SystemExit(main())
