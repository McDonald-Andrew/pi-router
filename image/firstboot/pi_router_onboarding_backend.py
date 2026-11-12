#!/usr/bin/python3
"""Privileged, one-time broker for Pi Router browser onboarding.

The browser frontend is deliberately unable to execute setup or mutate image
state.  It sends one bounded credential packet over a root-owned UNIX
``SOCK_SEQPACKET`` socket.  This broker authenticates the frontend with
``SO_PEERCRED``, records durable intent, and delegates the interactive setup
phase to :mod:`pi_router_onboarding_pseudo_terminal`.

No credential or raw pseudo-terminal output is logged, returned, or persisted.
All responses and durable receipts are fixed enums.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import os
import pwd
import re
import secrets
import signal
import socket
import stat
import struct
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ContextManager, Final, Iterator, Mapping, Sequence

from pi_router_onboarding_pseudo_terminal import (
    PseudoTerminalRunResult,
    PseudoTerminalStatus,
    run_post_reboot,
)
from pi_router_image_state import (
    Attempt,
    ERROR_CODES,
    STATUS_CODES as STATE_STATUS_CODES,
    ImageStateError,
    ImageStateStore,
    StateFormatError,
    validate_boot_id,
    validate_release_name,
    validate_sha256,
)

try:  # Linux-only production dependency.
    import fcntl
except ImportError:  # pragma: no cover - Windows only.
    fcntl = None  # type: ignore[assignment]


SOCKET_PATH: Final = Path("/run/pi-router-image/onboarding.sock")
RUNTIME_DIRECTORY: Final = SOCKET_PATH.parent
FIRSTBOOT_LOCK: Final = Path("/run/lock/pi-router-firstboot.lock")
STATE_DIRECTORY: Final = Path("/var/lib/pi-router-image")
CHECKPOINT_PATH: Final = Path(
    "/var/lib/pi-router-setup/reboot-required.boot-id"
)
BOOT_ID_PATH: Final = Path("/proc/sys/kernel/random/boot_id")
METADATA_PATH: Final = Path("/usr/share/pi-router/image-build/metadata")
CURRENT_RELEASE: Final = Path("/opt/pi-router/current")
RELEASES_DIRECTORY: Final = Path("/opt/pi-router/releases")
PROVISIONING_PATH: Final = Path(
    "/etc/dnsmasq.d/00-pi-router-provisioning.conf"
)
ROUTER_CONFIG_PATH: Final = Path("/etc/pi-router.conf")

FRONTEND_ACCOUNT: Final = "pi-router-onboarding"
SYSTEMCTL: Final = "/usr/bin/systemctl"
NFT: Final = "/usr/sbin/nft"
IP: Final = "/usr/sbin/ip"
OPENSSL: Final = "/usr/bin/openssl"
TLS_VALIDITY_DAYS: Final = 3650
LIVE_CHECK_TIMEOUT_SECONDS: Final = 3
SUCCESS_STATUS_GRACE_SECONDS: Final = 8.0

STATUS_REQUEST: Final = b"STATUS\n"
RESPONSE_ACCEPTED: Final = b"accepted\n"
RESPONSE_BUSY: Final = b"busy\n"
RESPONSE_NOT_READY: Final = b"not-ready\n"
RESPONSE_INVALID: Final = b"invalid\n"
RESPONSE_FAILED: Final = b"failed\n"

CREDENTIAL_VERSION: Final = 1
CREDENTIAL_HEADER_BYTES: Final = 5
MAX_USERNAME_BYTES: Final = 254
MAX_PASSWORD_BYTES: Final = 1024
MAX_CREDENTIAL_FRAME_BYTES: Final = (
    CREDENTIAL_HEADER_BYTES + MAX_USERNAME_BYTES + MAX_PASSWORD_BYTES
)
MAX_PACKET_BYTES: Final = MAX_CREDENTIAL_FRAME_BYTES

STATUS_CODES: Final = STATE_STATUS_CODES

_ACCEPT_RE: Final = re.compile(r"(?:^|\s)accept(?:\s|$)", re.MULTILINE)
_LAN_ASSIGNMENT_RE: Final = re.compile(
    r'''LAN_ADDR=(?:"([0-9.]+)"|'([0-9.]+)'|([0-9.]+))'''
)
_CONFIG_ASSIGNMENT_RE: Final = re.compile(
    r'([A-Z][A-Z0-9_]*)="([^"\r\n]*)"'
)
_INTERFACE_RE: Final = re.compile(r"[A-Za-z0-9_.:-]+")
_LIVE_ONBOARDING_CODES: Final = frozenset({"waiting-onboarding"})


def zero_bytearray(value: bytearray) -> None:
    """Best-effort overwrite of a mutable credential buffer."""

    value[:] = b"\x00" * len(value)


def _valid_utf8(value: memoryview) -> bool:
    """Validate UTF-8 directly over mutable storage without making a copy."""

    index = 0
    length = len(value)
    while index < length:
        first = value[index]
        if first <= 0x7F:
            index += 1
            continue
        if 0xC2 <= first <= 0xDF:
            needed = 1
            second_minimum, second_maximum = 0x80, 0xBF
        elif first == 0xE0:
            needed = 2
            second_minimum, second_maximum = 0xA0, 0xBF
        elif 0xE1 <= first <= 0xEC or 0xEE <= first <= 0xEF:
            needed = 2
            second_minimum, second_maximum = 0x80, 0xBF
        elif first == 0xED:
            needed = 2
            second_minimum, second_maximum = 0x80, 0x9F
        elif first == 0xF0:
            needed = 3
            second_minimum, second_maximum = 0x90, 0xBF
        elif 0xF1 <= first <= 0xF3:
            needed = 3
            second_minimum, second_maximum = 0x80, 0xBF
        elif first == 0xF4:
            needed = 3
            second_minimum, second_maximum = 0x80, 0x8F
        else:
            return False
        if index + needed >= length:
            return False
        if not second_minimum <= value[index + 1] <= second_maximum:
            return False
        for continuation_index in range(index + 2, index + needed + 1):
            if not 0x80 <= value[continuation_index] <= 0xBF:
                return False
        index += needed + 1
    return True


@dataclass(frozen=True, slots=True)
class BackendPaths:
    """Production paths, replaceable only by in-process tests."""

    root: Path = Path("/")

    def __post_init__(self) -> None:
        if not self.root.is_absolute():
            raise ValueError("backend root must be absolute")

    def path(self, absolute: Path | str) -> Path:
        value = os.fspath(absolute)
        if not value.startswith("/"):
            raise ValueError("backend paths must be absolute")
        if self.root == Path("/"):
            return Path(value)
        return self.root / value.removeprefix("/")

    @property
    def socket(self) -> Path:
        return self.path(SOCKET_PATH)

    @property
    def runtime_directory(self) -> Path:
        return self.path(RUNTIME_DIRECTORY)

    @property
    def lock(self) -> Path:
        return self.path(FIRSTBOOT_LOCK)

    @property
    def state_directory(self) -> Path:
        return self.path(STATE_DIRECTORY)

    @property
    def checkpoint(self) -> Path:
        return self.path(CHECKPOINT_PATH)

    @property
    def boot_id(self) -> Path:
        return self.path(BOOT_ID_PATH)

    @property
    def metadata(self) -> Path:
        return self.path(METADATA_PATH)

    @property
    def current_release(self) -> Path:
        return self.path(CURRENT_RELEASE)

    @property
    def releases_directory(self) -> Path:
        return self.path(RELEASES_DIRECTORY)

    @property
    def provisioning(self) -> Path:
        return self.path(PROVISIONING_PATH)

    @property
    def router_config(self) -> Path:
        return self.path(ROUTER_CONFIG_PATH)

    @property
    def certificate(self) -> Path:
        return self.runtime_directory / "onboarding.crt"

    @property
    def private_key(self) -> Path:
        return self.runtime_directory / "onboarding.key"


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    name: str
    setup_sha256: str

    def __post_init__(self) -> None:
        validate_release_name(self.name)
        validate_sha256(self.setup_sha256)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    output: str = ""


class CommandRunner:
    """Run fixed, credential-free backend verification commands."""

    def run(self, argv: Sequence[str], *, timeout: int = 30) -> CommandResult:
        try:
            completed = subprocess.run(
                tuple(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
                env={
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "HOME": "/root",
                    "USER": "root",
                    "LOGNAME": "root",
                },
            )
        except (OSError, subprocess.TimeoutExpired):
            return CommandResult(127, "")
        return CommandResult(
            completed.returncode,
            completed.stdout[: 256 * 1024].decode("utf-8", errors="replace"),
        )


class BackendFault(RuntimeError):
    """Sanitized terminal lifecycle fault."""

    def __init__(self, code: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"backend fault code is not in state schema: {code}")
        super().__init__(code)
        self.code = code


class NotReady(RuntimeError):
    """The valid one-time onboarding boundary is not currently present."""


@dataclass(frozen=True, slots=True)
class Preconditions:
    boot_id: str
    checkpoint: str


def _mode(metadata: os.stat_result) -> int:
    return stat.S_IMODE(metadata.st_mode)


def _decode_credentials(frame: bytearray) -> tuple[bytearray, bytearray]:
    """Strictly decode one credential frame into two wipeable buffers."""

    username = bytearray()
    password = bytearray()
    try:
        if not CREDENTIAL_HEADER_BYTES <= len(frame) <= MAX_CREDENTIAL_FRAME_BYTES:
            raise ValueError
        if frame[0] != CREDENTIAL_VERSION:
            raise ValueError
        username_length = (frame[1] << 8) | frame[2]
        password_length = (frame[3] << 8) | frame[4]
        if not 1 <= username_length <= MAX_USERNAME_BYTES:
            raise ValueError
        if not 1 <= password_length <= MAX_PASSWORD_BYTES:
            raise ValueError
        if len(frame) != CREDENTIAL_HEADER_BYTES + username_length + password_length:
            raise ValueError
        password_start = CREDENTIAL_HEADER_BYTES + username_length
        frame_view = memoryview(frame)
        username_source = frame_view[CREDENTIAL_HEADER_BYTES:password_start]
        password_source = frame_view[password_start:]
        try:
            username.extend(username_source)
            password.extend(password_source)
        finally:
            username_source.release()
            password_source.release()
            frame_view.release()
        if username.startswith(b"-"):
            raise ValueError
        if any(value == 0 or value < 0x20 or value == 0x7F for value in username):
            raise ValueError
        if any(value < 0x20 or value == 0x7F for value in password):
            raise ValueError
        username_view = memoryview(username)
        password_view = memoryview(password)
        try:
            if not _valid_utf8(username_view) or not _valid_utf8(password_view):
                raise ValueError
        finally:
            username_view.release()
            password_view.release()
        return username, password
    except Exception as exc:
        zero_bytearray(username)
        zero_bytearray(password)
        raise ValueError("invalid credential frame") from exc


class OnboardingBackend:
    """State-aware credential broker independent of the socket accept loop."""

    def __init__(
        self,
        *,
        paths: BackendPaths | None = None,
        store: ImageStateStore | None = None,
        runner: CommandRunner | None = None,
        pseudo_terminal_runner: Callable[
            [bytearray, bytearray], PseudoTerminalRunResult
        ] = run_post_reboot,
        worker_launcher: Callable[[Callable[[], None]], None] | None = None,
        lock_factory: Callable[[], ContextManager[None]] | None = None,
        expected_uid: int | None = 0,
        expected_gid: int | None = 0,
        boot_id_override: str | None = None,
        release_override: ReleaseInfo | None = None,
        success_status_grace_seconds: float = SUCCESS_STATUS_GRACE_SECONDS,
    ) -> None:
        if success_status_grace_seconds < 0:
            raise ValueError("success status grace must not be negative")
        self.paths = paths or BackendPaths()
        self.store = store or ImageStateStore(
            self.paths.state_directory,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        self.runner = runner or CommandRunner()
        self.pseudo_terminal_runner = pseudo_terminal_runner
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self.boot_id_override = boot_id_override
        self.release_override = release_override
        self.success_status_grace_seconds = success_status_grace_seconds
        self._worker_launcher = worker_launcher or self._launch_thread
        self._lock_factory = lock_factory or self._firstboot_lock
        self._guard = threading.Lock()
        self._worker_active = False
        self._accepting = True
        self.stop_event = threading.Event()

    @staticmethod
    def _launch_thread(target: Callable[[], None]) -> None:
        worker = threading.Thread(
            target=target,
            name="pi-router-onboarding-worker",
            daemon=False,
        )
        worker.start()

    def _owner_ok(self, metadata: os.stat_result) -> bool:
        return (
            (self.expected_uid is None or metadata.st_uid == self.expected_uid)
            and (self.expected_gid is None or metadata.st_gid == self.expected_gid)
        )

    def _read_regular(
        self,
        path: Path,
        *,
        mode: int,
        maximum: int,
        code: str,
    ) -> str:
        try:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or _mode(metadata) != mode
                or not self._owner_ok(metadata)
                or metadata.st_size > maximum
            ):
                raise BackendFault(code)
            return path.read_bytes().decode("ascii")
        except BackendFault:
            raise
        except (OSError, UnicodeDecodeError) as exc:
            raise BackendFault(code) from exc

    def _current_boot_id(self) -> str:
        if self.boot_id_override is not None:
            try:
                return validate_boot_id(self.boot_id_override, "current boot ID")
            except StateFormatError as exc:
                raise BackendFault("invalid-state") from exc
        value = self._read_regular(
            self.paths.boot_id,
            mode=0o444,
            maximum=64,
            code="invalid-state",
        )
        try:
            return validate_boot_id(value.strip(), "current boot ID")
        except StateFormatError as exc:
            raise BackendFault("invalid-state") from exc

    def _checkpoint(self) -> str:
        try:
            metadata = self.paths.checkpoint.lstat()
        except FileNotFoundError as exc:
            raise BackendFault("checkpoint-missing") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or _mode(metadata) != 0o600
            or not self._owner_ok(metadata)
            or metadata.st_size > 64
        ):
            raise BackendFault("checkpoint-corrupt")
        try:
            value = self.paths.checkpoint.read_bytes().decode("ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise BackendFault("checkpoint-corrupt") from exc
        if value.count("\n") != 1 or not value.endswith("\n"):
            raise BackendFault("checkpoint-corrupt")
        try:
            return validate_boot_id(value[:-1], "setup checkpoint")
        except StateFormatError as exc:
            raise BackendFault("checkpoint-corrupt") from exc

    def _preconditions(self) -> Preconditions:
        try:
            if self.store.read_state() != "pre-reboot-complete":
                raise NotReady
            if self.store.read_error() is not None or self.store.read_attempt() is not None:
                raise NotReady
            boot_id = self._current_boot_id()
            checkpoint = self._checkpoint()
        except ImageStateError as exc:
            raise BackendFault("invalid-state") from exc
        if checkpoint == boot_id:
            raise NotReady
        return Preconditions(boot_id=boot_id, checkpoint=checkpoint)

    def _interface_roles(self) -> tuple[str, str]:
        """Read the setup-detected LAN/WAN roles without detecting hardware."""

        text = self._read_regular(
            self.paths.router_config,
            mode=0o644,
            maximum=8192,
            code="invalid-config",
        )
        fields: dict[str, str] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = _CONFIG_ASSIGNMENT_RE.fullmatch(line)
            if match is None:
                raise BackendFault("invalid-config")
            key, value = match.groups()
            if key in fields:
                raise BackendFault("invalid-config")
            fields[key] = value
        try:
            lan_interface = fields["LAN_IF"]
            wan_interface = fields["WAN_IF"]
        except KeyError as exc:
            raise BackendFault("invalid-config") from exc
        if (
            _INTERFACE_RE.fullmatch(lan_interface) is None
            or _INTERFACE_RE.fullmatch(wan_interface) is None
            or lan_interface == wan_interface
        ):
            raise BackendFault("invalid-config")
        return lan_interface, wan_interface

    def _carrier(self, interface: str) -> bool:
        path = self.paths.path(f"/sys/class/net/{interface}/carrier")
        try:
            with path.open("rb") as stream:
                value = stream.read(8)
                if stream.read(1):
                    return False
        except OSError:
            return False
        return value.strip() == b"1"

    def _wan_route(self, interface: str) -> bool:
        result = self.runner.run(
            (IP, "-4", "route", "show", "default", "dev", interface),
            timeout=LIVE_CHECK_TIMEOUT_SECONDS,
        )
        return result.returncode == 0 and bool(result.output.strip())

    def _live_prerequisite_status(self) -> str:
        """Return a fixed status for current, non-secret network readiness."""

        lan_interface, wan_interface = self._interface_roles()
        if not self._carrier(lan_interface):
            return "waiting-lan-carrier"
        if not self._carrier(wan_interface):
            return "waiting-wan-carrier"
        if not self._wan_route(wan_interface):
            return "waiting-wan-dhcp"
        return "waiting-onboarding"

    def _verify_release(self) -> ReleaseInfo:
        if self.release_override is not None:
            return self.release_override
        text = self._read_regular(
            self.paths.metadata,
            mode=0o644,
            maximum=4096,
            code="release-integrity",
        )
        fields: dict[str, str] = {}
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            if separator != "=" or not key or not value or key in fields:
                raise BackendFault("release-integrity")
            fields[key] = value
        try:
            release = ReleaseInfo(
                name=fields["release_name"],
                setup_sha256=fields["release_setup_sha256"],
            )
        except (KeyError, StateFormatError) as exc:
            raise BackendFault("release-integrity") from exc
        expected_link = f"releases/{release.name}"
        try:
            if (
                not self.paths.current_release.is_symlink()
                or os.readlink(self.paths.current_release) != expected_link
            ):
                raise BackendFault("release-integrity")
        except OSError as exc:
            raise BackendFault("release-integrity") from exc
        setup = self.paths.releases_directory / release.name / "setup"
        try:
            metadata = setup.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or not self._owner_ok(metadata)
                or not (_mode(metadata) & 0o111)
            ):
                raise BackendFault("release-integrity")
            actual = hashlib.sha256(setup.read_bytes()).hexdigest()
        except BackendFault:
            raise
        except OSError as exc:
            raise BackendFault("release-integrity") from exc
        if actual != release.setup_sha256:
            raise BackendFault("release-integrity")
        return release

    @contextlib.contextmanager
    def _firstboot_lock(self) -> Iterator[None]:
        if fcntl is None:  # pragma: no cover - production is Linux.
            raise BackendFault("internal-error")
        path = self.paths.lock
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise BackendFault("internal-error") from exc
        try:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or not self._owner_ok(metadata)
            ):
                raise BackendFault("internal-error")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _bootstrap_closed(self) -> bool:
        result = self.runner.run(
            (
                NFT,
                "-a",
                "list",
                "chain",
                "inet",
                "router_filter",
                "host_wan_bootstrap",
            )
        )
        return result.returncode == 0 and _ACCEPT_RE.search(result.output) is None

    def _provisioning_absent(self) -> bool:
        try:
            self.paths.provisioning.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    def _terminal(self, code: str) -> None:
        if code not in ERROR_CODES:
            code = "internal-error"
        try:
            state = self.store.read_state()
            self.store.write_error(state, code)
            self.store.write_status(state, "error")
        finally:
            with self._guard:
                self._accepting = False
            self.stop_event.set()

    def _retry(self) -> None:
        self.store.clear_attempt()
        self.store.write_status("pre-reboot-complete", "waiting-onboarding")

    def _commit_post(self, boot_id: str) -> None:
        if self._checkpoint() != boot_id:
            raise BackendFault("checkpoint-conflict")
        if not self._provisioning_absent():
            raise BackendFault("setup-post-failed")
        if not self._bootstrap_closed():
            raise BackendFault("setup-post-failed")
        self.store.write_state("post-reboot-complete")
        self.store.clear_attempt()
        self.store.write_status("post-reboot-complete", "rebooting-after-post")
        with self._guard:
            self._accepting = False
            # Expose the committed success status while the broker remains
            # available so the browser can arm its automatic HTTP handoff.
            self._worker_active = False
        self.stop_event.wait(self.success_status_grace_seconds)
        self.runner.run((SYSTEMCTL, "--no-block", "reboot"))
        self.stop_event.set()

    def _run_worker(self, username: bytearray, password: bytearray) -> None:
        attempted = False
        prior: Preconditions | None = None
        result: PseudoTerminalRunResult | None = None
        fault: BackendFault | None = None
        try:
            with self._lock_factory():
                prior = self._preconditions()
                release = self._verify_release()
                self.store.write_attempt(
                    Attempt(
                        operation="post",
                        invocation_boot_id=prior.boot_id,
                        checkpoint_before=prior.checkpoint,
                        release_name=release.name,
                        release_setup_sha256=release.setup_sha256,
                        late_apply_before="not-applicable",
                        original_pre_boot_id="not-applicable",
                    )
                )
                attempted = True
                self.store.write_status(
                    "pre-reboot-complete", "running-post-reboot"
                )
                try:
                    result = self.pseudo_terminal_runner(username, password)
                except Exception:
                    result = None
                finally:
                    zero_bytearray(username)
                    zero_bytearray(password)

                try:
                    after = self._checkpoint()
                except BackendFault as exc:
                    fault = exc
                else:
                    if after == prior.boot_id:
                        self._commit_post(prior.boot_id)
                    elif after != prior.checkpoint:
                        fault = BackendFault("checkpoint-conflict")
                    elif not self._bootstrap_closed():
                        fault = BackendFault("setup-post-failed")
                    elif (
                        result is not None
                        and result.status is PseudoTerminalStatus.SUCCESS
                    ):
                        fault = BackendFault("setup-post-failed")
                    else:
                        self._retry()
        except NotReady:
            # A controller transition won the race before durable intent.
            pass
        except BackendFault as exc:
            fault = exc
        except ImageStateError:
            fault = BackendFault("invalid-state")
        except Exception:
            fault = BackendFault("internal-error")
        finally:
            zero_bytearray(username)
            zero_bytearray(password)
            if fault is not None:
                try:
                    self._terminal(fault.code)
                except Exception:
                    self.stop_event.set()
            elif attempted and prior is not None:
                # _commit_post and _retry are the only nonfault exits after
                # durable intent.  Both consume the attempt record.
                try:
                    if self.store.read_attempt() is not None:
                        self._terminal("internal-error")
                except Exception:
                    self.stop_event.set()
            with self._guard:
                self._worker_active = False

    def status_code(self) -> str:
        """Return only a frontend-approved, non-secret status enum."""

        with self._guard:
            if self._worker_active:
                return "running-post-reboot"
        try:
            if self.store.read_error() is not None:
                return "error"
            status = self.store.read_status()
            state = self.store.read_state()
            defaults: Mapping[str, str] = {
                "new": "initializing-identity",
                "pre-reboot-complete": "waiting-onboarding",
                "post-reboot-complete": "rebooting-after-post",
                "ready": "ready",
            }
            code = (
                status.code
                if status is not None and status.code in STATUS_CODES
                else defaults.get(state, "error")
            )
            if state == "pre-reboot-complete" and code in _LIVE_ONBOARDING_CODES:
                return self._live_prerequisite_status()
            return code
        except (BackendFault, ImageStateError):
            return "error"

    def submit_frame(self, frame: bytearray) -> bytes:
        """Validate and accept one mutable credential frame."""

        username = bytearray()
        password = bytearray()
        try:
            try:
                username, password = _decode_credentials(frame)
            except ValueError:
                return RESPONSE_INVALID
            zero_bytearray(frame)
            with self._guard:
                if self._worker_active:
                    return RESPONSE_BUSY
                if not self._accepting:
                    return RESPONSE_NOT_READY
            try:
                self._preconditions()
                if self._live_prerequisite_status() != "waiting-onboarding":
                    return RESPONSE_NOT_READY
            except NotReady:
                return RESPONSE_NOT_READY
            except BackendFault as exc:
                try:
                    self._terminal(exc.code)
                except Exception:
                    self.stop_event.set()
                return RESPONSE_FAILED
            with self._guard:
                if self._worker_active:
                    return RESPONSE_BUSY
                if not self._accepting:
                    return RESPONSE_NOT_READY
                self._worker_active = True
            try:
                worker_username = username
                worker_password = password
                self._worker_launcher(
                    lambda u=worker_username, p=worker_password: self._run_worker(u, p)
                )
            except Exception:
                with self._guard:
                    self._worker_active = False
                return RESPONSE_FAILED
            # Ownership of both buffers has moved to the worker.
            username = bytearray()
            password = bytearray()
            return RESPONSE_ACCEPTED
        finally:
            zero_bytearray(frame)
            zero_bytearray(username)
            zero_bytearray(password)


class BrokerServer:
    """Authenticated SOCK_SEQPACKET accept loop for the unprivileged UI."""

    def __init__(
        self,
        backend: OnboardingBackend,
        *,
        frontend_uid: int,
        frontend_gid: int,
        owner_uid: int = 0,
        paths: BackendPaths | None = None,
    ) -> None:
        self.backend = backend
        self.frontend_uid = frontend_uid
        self.frontend_gid = frontend_gid
        self.owner_uid = owner_uid
        self.paths = paths or backend.paths
        self.listener: socket.socket | None = None

    def _prepare_directory(self) -> None:
        path = self.paths.runtime_directory
        created = False
        try:
            path.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise RuntimeError("unsafe onboarding runtime directory") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("unsafe onboarding runtime directory")
            if created:
                # Only a directory created by this process may be normalized.
                # A preexisting path is validation-only and is never repaired.
                if metadata.st_uid != self.owner_uid:
                    raise RuntimeError("unsafe new onboarding runtime directory")
                os.fchown(descriptor, self.owner_uid, self.frontend_gid)
                os.fchmod(descriptor, 0o750)
                os.fsync(descriptor)
                metadata = os.fstat(descriptor)
            if (
                metadata.st_uid != self.owner_uid
                or metadata.st_gid != self.frontend_gid
                or _mode(metadata) != 0o750
            ):
                raise RuntimeError("unsafe onboarding runtime directory")
        finally:
            os.close(descriptor)
        if created:
            parent = os.open(path.parent, flags)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)

    def _remove_stale_socket(self) -> None:
        path = self.paths.socket
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != self.owner_uid
            or metadata.st_gid != self.frontend_gid
            or _mode(metadata) != 0o660
        ):
            raise RuntimeError("unsafe existing onboarding socket")
        path.unlink()

    def open(self) -> None:
        self._prepare_directory()
        self._remove_stale_socket()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            listener.bind(str(self.paths.socket))
            os.chown(self.paths.socket, self.owner_uid, self.frontend_gid)
            os.chmod(self.paths.socket, 0o660)
            listener.listen(8)
            listener.settimeout(1.0)
        except Exception:
            listener.close()
            raise
        self.listener = listener

    def _peer_allowed(self, connection: socket.socket) -> bool:
        if not hasattr(socket, "SO_PEERCRED"):
            return False
        try:
            raw = connection.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            _pid, uid, gid = struct.unpack("3i", raw)
        except (OSError, struct.error):
            return False
        return uid == self.frontend_uid and gid == self.frontend_gid

    def handle_connection(self, connection: socket.socket) -> None:
        packet = bytearray(MAX_PACKET_BYTES + 1)
        packet_view = memoryview(packet)
        view_released = False
        try:
            connection.settimeout(5.0)
            if not self._peer_allowed(connection):
                return
            received, _ancillary, flags, _address = connection.recvmsg_into(
                (packet_view,), 0
            )
            packet_view.release()
            view_released = True
            if flags & getattr(socket, "MSG_TRUNC", 0) or received > MAX_PACKET_BYTES:
                response = RESPONSE_INVALID
            elif received == len(STATUS_REQUEST) and all(
                packet[index] == expected
                for index, expected in enumerate(STATUS_REQUEST)
            ):
                response = (self.backend.status_code() + "\n").encode("ascii")
            else:
                del packet[received:]
                response = self.backend.submit_frame(packet)
            connection.send(response)
        except (OSError, TimeoutError):
            pass
        finally:
            if not view_released:
                packet_view.release()
            zero_bytearray(packet)

    def serve(self) -> None:
        if self.listener is None:
            self.open()
        assert self.listener is not None
        try:
            while not self.backend.stop_event.is_set():
                try:
                    connection, _address = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.backend.stop_event.is_set():
                        break
                    raise
                with connection:
                    self.handle_connection(connection)
        finally:
            self.close()

    def close(self) -> None:
        if self.listener is not None:
            try:
                self.listener.close()
            finally:
                self.listener = None
        try:
            metadata = self.paths.socket.lstat()
            if (
                stat.S_ISSOCK(metadata.st_mode)
                and metadata.st_uid == self.owner_uid
            ):
                self.paths.socket.unlink()
        except FileNotFoundError:
            pass


class TLSMaterial:
    """Create one per-boot self-signed certificate in the runtime directory."""

    def __init__(
        self,
        paths: BackendPaths,
        *,
        runner: CommandRunner | None = None,
        expected_uid: int | None = 0,
        expected_gid: int | None = 0,
    ) -> None:
        self.paths = paths
        self.runner = runner or CommandRunner()
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid

    def _lan_address(self) -> str:
        path = self.paths.router_config
        try:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or _mode(metadata) != 0o644
                or (self.expected_uid is not None and metadata.st_uid != self.expected_uid)
                or (self.expected_gid is not None and metadata.st_gid != self.expected_gid)
                or metadata.st_size > 16 * 1024
            ):
                raise RuntimeError("unsafe router config")
            text = path.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError("router config is unavailable") from exc
        values: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or not line.startswith("LAN_ADDR="):
                continue
            match = _LAN_ASSIGNMENT_RE.fullmatch(line)
            if match is None:
                raise RuntimeError("invalid LAN address assignment")
            values.append(next(value for value in match.groups() if value is not None))
        if len(values) != 1:
            raise RuntimeError("router config must contain one LAN address")
        try:
            address = ipaddress.IPv4Address(values[0])
        except ipaddress.AddressValueError as exc:
            raise RuntimeError("invalid LAN address") from exc
        if address.is_unspecified or address.is_loopback or address.is_multicast:
            raise RuntimeError("unsafe LAN address")
        return str(address)

    def _validate_existing(self, path: Path, mode: int) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or _mode(metadata) != mode
        ):
            raise RuntimeError("unsafe existing TLS material")

    @staticmethod
    def _fsync(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def create(self) -> None:
        address = self._lan_address()
        self._validate_existing(self.paths.certificate, 0o644)
        self._validate_existing(self.paths.private_key, 0o600)
        suffix = secrets.token_hex(16)
        temporary_certificate = self.paths.runtime_directory / f".onboarding.crt.{suffix}"
        temporary_key = self.paths.runtime_directory / f".onboarding.key.{suffix}"
        try:
            result = self.runner.run(
                (
                    OPENSSL,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:3072",
                    "-sha256",
                    "-nodes",
                    "-days",
                    str(TLS_VALIDITY_DAYS),
                    "-subj",
                    "/CN=Pi Router onboarding",
                    "-addext",
                    f"subjectAltName=IP:{address}",
                    "-keyout",
                    str(temporary_key),
                    "-out",
                    str(temporary_certificate),
                ),
                timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError("TLS generation failed")
            for path, mode in (
                (temporary_certificate, 0o644),
                (temporary_key, 0o600),
            ):
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size == 0:
                    raise RuntimeError("TLS generation produced unsafe output")
                os.chown(path, 0, 0)
                os.chmod(path, mode)
                self._fsync(path)
            os.replace(temporary_certificate, self.paths.certificate)
            os.replace(temporary_key, self.paths.private_key)
            directory = os.open(
                self.paths.runtime_directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            for path in (temporary_certificate, temporary_key):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass


def sd_notify_ready() -> None:
    """Send READY=1 to systemd without a third-party binding."""

    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address:
        raise RuntimeError("NOTIFY_SOCKET is unavailable")
    if address.startswith("@"):  # systemd abstract namespace spelling.
        address = "\0" + address[1:]
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.connect(address)
        client.sendall(b"READY=1\nSTATUS=Waiting for Proton onboarding")
    finally:
        client.close()


def remove_runtime_tls(paths: BackendPaths) -> None:
    """Remove only backend-created, root-owned TLS files."""

    changed = False
    for path, mode in ((paths.certificate, 0o644), (paths.private_key, 0o600)):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or _mode(metadata) != mode
        ):
            raise RuntimeError("refusing unsafe runtime TLS cleanup")
        path.unlink()
        changed = True
    if changed:
        descriptor = os.open(
            paths.runtime_directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _frontend_identity() -> tuple[int, int]:
    account = pwd.getpwnam(FRONTEND_ACCOUNT)
    return account.pw_uid, account.pw_gid


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        import sys

        argv = sys.argv[1:]
    if argv:
        return 2
    os.umask(0o077)
    backend: OnboardingBackend | None = None
    try:
        backend = OnboardingBackend()
        uid, gid = _frontend_identity()
        # Refuse to expose a socket outside the exact onboarding boundary.
        try:
            backend._preconditions()
        except NotReady:
            return 0
        server = BrokerServer(
            backend,
            frontend_uid=uid,
            frontend_gid=gid,
        )

        def stop(_signum: int, _frame: object) -> None:
            backend.stop_event.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            server.open()
            TLSMaterial(backend.paths, runner=backend.runner).create()
            sd_notify_ready()
            server.serve()
        finally:
            server.close()
            remove_runtime_tls(backend.paths)
        return 0
    except BackendFault as exc:
        if backend is not None:
            try:
                backend._terminal(exc.code)
            except Exception:
                pass
        return 1
    except ImageStateError:
        if backend is not None:
            try:
                backend._terminal("invalid-state")
            except Exception:
                pass
        return 1
    except (KeyError, OSError, RuntimeError):
        if backend is not None:
            try:
                backend._terminal("internal-error")
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
