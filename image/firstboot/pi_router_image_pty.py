#!/usr/bin/python3
"""Credential-safe PTY runner for the image-owned Proton onboarding step.

The production command and environment in this module are intentionally fixed.
Tests may inject another absolute command and working directory, but callers in
the appliance should use :func:`run_post_reboot` without those overrides.

Raw PTY output is never returned or logged.  Only prompt/marker booleans and an
enumerated terminal status leave this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import errno
import os
import select
import signal
import struct
import time
from typing import Mapping, Optional, Sequence

try:  # These modules do not exist on Windows, where the runner is unsupported.
    import fcntl
    import termios
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts.
    fcntl = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]


SETUP_ARGV = (
    "/opt/pi-router/current/setup",
    "--live",
    "--allow-live-install",
    "--phase",
    "post-reboot",
)
SETUP_CWD = "/opt/pi-router/current"

# Do not inherit service-manager or caller environment variables.  In
# particular, credentials and askpass settings must never reach setup.
SETUP_ENV: Mapping[str, str] = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TERM": "dumb",
    "HOME": "/root",
    "USER": "root",
    "LOGNAME": "root",
}


class PtyStatus(str, Enum):
    """Sanitized terminal status for one post-reboot invocation."""

    SUCCESS = "success"
    INVALID_CREDENTIAL = "invalid-credential"
    UNSUPPORTED_PLATFORM = "unsupported-platform"
    SPAWN_FAILED = "spawn-failed"
    IO_ERROR = "pty-io-error"
    COMMAND_FAILED = "command-failed"
    MISSING_SUCCESS_MARKER = "missing-success-marker"
    USERNAME_PROMPT_TIMEOUT = "username-prompt-timeout"
    PASSWORD_PROMPT_TIMEOUT = "password-prompt-timeout"
    TOTAL_TIMEOUT = "total-timeout"
    REPEATED_USERNAME_PROMPT = "repeated-username-prompt"
    REPEATED_PASSWORD_PROMPT = "repeated-password-prompt"
    UNEXPECTED_PASSWORD_PROMPT = "unexpected-password-prompt"
    UNSUPPORTED_AUTH_FLOW = "unsupported-auth-flow"
    UNEXPECTED_SUDO_PROMPT = "unexpected-sudo-prompt"


@dataclass(frozen=True)
class PtyTimeouts:
    """All timeout values are monotonic seconds and must be positive."""

    total: float = 20 * 60
    username_prompt: float = 2 * 60
    password_prompt: float = 2 * 60
    write: float = 5
    terminate_grace: float = 30
    kill_grace: float = 5
    poll_interval: float = 0.05

    def validate(self) -> None:
        for name, value in self.__dict__.items():
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} timeout must be positive")


@dataclass(frozen=True)
class PtyRunResult:
    """A credential-free summary of an onboarding PTY invocation."""

    status: PtyStatus
    exit_code: Optional[int]
    username_prompt_seen: bool
    password_prompt_seen: bool
    signin_started: bool
    authenticated_session_detected: bool
    username_sent: bool
    password_sent: bool
    proton_managed_connection_verified: bool
    proton0_wireguard_verified: bool
    bootstrap_firewall_verified_closed: bool
    post_reboot_stage_complete: bool
    final_reboot_required: bool
    termination_requested: bool
    kill_required: bool
    scan_buffer_peak: int

    @property
    def all_success_markers(self) -> bool:
        return all(
            (
                self.proton_managed_connection_verified,
                self.proton0_wireguard_verified,
                self.bootstrap_firewall_verified_closed,
                self.post_reboot_stage_complete,
                self.final_reboot_required,
            )
        )


@dataclass
class _Observed:
    username_prompt_seen: bool = False
    password_prompt_seen: bool = False
    signin_started: bool = False
    authenticated_session_detected: bool = False
    username_sent: bool = False
    password_sent: bool = False
    proton_managed_connection_verified: bool = False
    proton0_wireguard_verified: bool = False
    bootstrap_firewall_verified_closed: bool = False
    post_reboot_stage_complete: bool = False
    final_reboot_required: bool = False


_USERNAME_PROMPT = b"proton account username or email:"
_PASSWORD_PROMPT = b"password:"
_SIGNIN_MARKER = b"setup: starting interactive proton sign-in"
_AUTHENTICATED_MARKER = (
    b"setup: authenticated proton session detected; sign-in skipped"
)
_SUCCESS_TOKENS = {
    "proton_managed_connection_verified": b"proton managed connection verified",
    "proton0_wireguard_verified": b"proton0 wireguard interface verified",
    "bootstrap_firewall_verified_closed": b"bootstrap firewall verified closed",
    "post_reboot_stage_complete": b"post-reboot stage complete",
    "final_reboot_required": (
        b"final reboot required for unattended verification"
    ),
}
_UNSUPPORTED_AUTH_TOKENS = (
    b"two-factor",
    b"two factor",
    b"2fa",
    b"totp",
    b"one-time password",
    b"one time password",
    b"verification code",
    b"authenticator code",
    b"security key",
    b"u2f",
    b"fido",
)
_SUDO_PROMPT = b"[sudo] password for"
_MAX_USERNAME_BYTES = 254
_MAX_PASSWORD_BYTES = 4096


class _BoundedScanner:
    """Bounded ASCII-case-folded PTY window with absolute token positions."""

    def __init__(self, limit: int) -> None:
        longest = max(
            len(token)
            for token in (
                _USERNAME_PROMPT,
                _PASSWORD_PROMPT,
                _SIGNIN_MARKER,
                _AUTHENTICATED_MARKER,
                _SUDO_PROMPT,
                *_SUCCESS_TOKENS.values(),
                *_UNSUPPORTED_AUTH_TOKENS,
            )
        )
        if limit < longest * 2:
            raise ValueError("scan buffer is too small for split prompt detection")

        self.limit = limit
        self.data = bytearray()
        self.base_offset = 0
        self.peak = 0

    def feed(self, chunk: bytes) -> None:
        # Store only a mutable, ASCII-lowercased representation.  There is no
        # reason for this security boundary to retain display-ready output.
        self.data.extend(
            value + 32 if 65 <= value <= 90 else value for value in chunk
        )
        if len(self.data) > self.limit:
            extra = len(self.data) - self.limit
            _zero_prefix(self.data, extra)
            del self.data[:extra]
            self.base_offset += extra
        self.peak = max(self.peak, len(self.data))

    def contains(self, token: bytes) -> bool:
        return self.data.find(token) >= 0

    def positions_after(self, token: bytes, absolute_position: int) -> list[int]:
        relative = max(0, absolute_position - self.base_offset + 1)
        found: list[int] = []
        while True:
            relative = self.data.find(token, relative)
            if relative < 0:
                return found
            found.append(self.base_offset + relative)
            relative += max(1, len(token))

    def zero(self) -> None:
        _zero_bytearray(self.data)
        self.data.clear()


def _zero_prefix(value: bytearray, length: int) -> None:
    for index in range(min(length, len(value))):
        value[index] = 0


def _zero_bytearray(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


def _credentials_valid(username: bytearray, password: bytearray) -> bool:
    if not 1 <= len(username) <= _MAX_USERNAME_BYTES:
        return False
    if not 1 <= len(password) <= _MAX_PASSWORD_BYTES:
        return False
    return not any(value < 0x20 or value == 0x7F for value in username) and not any(
        value < 0x20 or value == 0x7F for value in password
    )


def _make_result(
    status: PtyStatus,
    exit_code: Optional[int],
    observed: _Observed,
    *,
    termination_requested: bool,
    kill_required: bool,
    scan_buffer_peak: int,
) -> PtyRunResult:
    return PtyRunResult(
        status=status,
        exit_code=exit_code,
        username_prompt_seen=observed.username_prompt_seen,
        password_prompt_seen=observed.password_prompt_seen,
        signin_started=observed.signin_started,
        authenticated_session_detected=(
            observed.authenticated_session_detected
        ),
        username_sent=observed.username_sent,
        password_sent=observed.password_sent,
        proton_managed_connection_verified=(
            observed.proton_managed_connection_verified
        ),
        proton0_wireguard_verified=observed.proton0_wireguard_verified,
        bootstrap_firewall_verified_closed=(
            observed.bootstrap_firewall_verified_closed
        ),
        post_reboot_stage_complete=observed.post_reboot_stage_complete,
        final_reboot_required=observed.final_reboot_required,
        termination_requested=termination_requested,
        kill_required=kill_required,
        scan_buffer_peak=scan_buffer_peak,
    )


def _spawn_pty(command: tuple[str, ...], cwd: str) -> tuple[int, int]:
    if fcntl is None or termios is None or not hasattr(os, "fork"):
        raise OSError(errno.ENOSYS, "POSIX PTY support is unavailable")

    master_fd, slave_fd = os.openpty()
    try:
        attributes = termios.tcgetattr(slave_fd)
        attributes[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(slave_fd, termios.TCSANOW, attributes)

        # Give terminal-aware clients a stable, ordinary window size.
        fcntl.ioctl(
            slave_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 24, 80, 0, 0),
        )

        pid = os.fork()
        if pid == 0:  # pragma: no branch - the child immediately execs.
            try:
                os.setsid()
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                if master_fd > 2:
                    os.close(master_fd)
                if slave_fd > 2:
                    os.close(slave_fd)
                os.chdir(cwd)
                os.execve(command[0], command, dict(SETUP_ENV))
            except BaseException:
                # This fixed message contains neither exception text nor input.
                try:
                    os.write(2, b"pi-router-image-pty: exec failed\n")
                finally:
                    os._exit(127)

        os.close(slave_fd)
        os.set_blocking(master_fd, False)
        return pid, master_fd
    except BaseException:
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            os.close(slave_fd)
        except OSError:
            pass
        raise


def _poll_child(pid: int) -> Optional[int]:
    try:
        waited, wait_status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return 0
    if waited == 0:
        return None
    return os.waitstatus_to_exitcode(wait_status)


def _write_line(fd: int, value: bytearray, deadline: float) -> None:
    view = memoryview(value)
    try:
        offset = 0
        while offset < len(view):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            _, writable, _ = select.select([], [fd], [], remaining)
            if not writable:
                raise TimeoutError
            try:
                written = os.write(fd, view[offset:])
            except BlockingIOError:
                continue
            if written <= 0:
                raise OSError(errno.EIO, "PTY write failed")
            offset += written

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        _, writable, _ = select.select([], [fd], [], remaining)
        if not writable:
            raise TimeoutError
        os.write(fd, b"\n")
    finally:
        view.release()


def _signal_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
        return
    except ProcessLookupError:
        pass
    except PermissionError:
        pass

    # The child may not yet have completed setsid().  Never signal the
    # controller's own process group in that short race.
    try:
        if os.getpgid(pid) != os.getpgrp():
            os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _read_once(fd: int, scanner: _BoundedScanner) -> bool:
    """Read one bounded chunk; return False after PTY EOF."""

    try:
        # Never read more than half the retained window at once.  Combined
        # with the two-token minimum window, this preserves a split token long
        # enough for the caller to inspect it even during an output flood.
        chunk = os.read(fd, min(4096, max(1, scanner.limit // 2)))
    except BlockingIOError:
        return True
    except OSError as error:
        if error.errno == errno.EIO:
            return False
        raise
    if not chunk:
        return False
    scanner.feed(chunk)
    # The immutable result from os.read cannot be overwritten in Python;
    # dropping the only reference immediately is the available boundary.
    del chunk
    return True


def _refresh_markers(scanner: _BoundedScanner, observed: _Observed) -> None:
    observed.signin_started = (
        observed.signin_started or scanner.contains(_SIGNIN_MARKER)
    )
    observed.authenticated_session_detected = (
        observed.authenticated_session_detected
        or scanner.contains(_AUTHENTICATED_MARKER)
    )
    for field, token in _SUCCESS_TOKENS.items():
        if scanner.contains(token):
            setattr(observed, field, True)


def _terminate_process_group(
    pid: int,
    master_fd: int,
    scanner: _BoundedScanner,
    timeouts: PtyTimeouts,
    exit_code: Optional[int],
) -> tuple[Optional[int], bool]:
    """TERM then KILL the owned process group within fixed deadlines."""

    _signal_group(pid, signal.SIGTERM)
    term_deadline = time.monotonic() + timeouts.terminate_grace

    while time.monotonic() < term_deadline:
        if exit_code is None:
            exit_code = _poll_child(pid)
        try:
            readable, _, _ = select.select(
                [master_fd], [], [], timeouts.poll_interval
            )
            if readable:
                _read_once(master_fd, scanner)
        except (OSError, ValueError):
            pass
        if exit_code is not None and not _group_exists(pid):
            return exit_code, False

    kill_required = _group_exists(pid) or exit_code is None
    if kill_required:
        _signal_group(pid, signal.SIGKILL)

    kill_deadline = time.monotonic() + timeouts.kill_grace
    while time.monotonic() < kill_deadline:
        if exit_code is None:
            exit_code = _poll_child(pid)
        if exit_code is not None and not _group_exists(pid):
            break
        try:
            readable, _, _ = select.select(
                [master_fd], [], [], timeouts.poll_interval
            )
            if readable:
                _read_once(master_fd, scanner)
        except (OSError, ValueError):
            pass

    if exit_code is None:
        # SIGKILL makes the direct child waitable; reap it so no zombie is
        # left behind even if the process-group existence probe is delayed.
        try:
            _, wait_status = os.waitpid(pid, 0)
            exit_code = os.waitstatus_to_exitcode(wait_status)
        except ChildProcessError:
            exit_code = 0

    return exit_code, kill_required


def run_post_reboot(
    username: bytearray,
    password: bytearray,
    *,
    command: Sequence[str] = SETUP_ARGV,
    cwd: str = SETUP_CWD,
    timeouts: PtyTimeouts = PtyTimeouts(),
    max_scan_buffer: int = 16 * 1024,
) -> PtyRunResult:
    """Run the canonical post-reboot phase through a credential-safe PTY.

    ``username`` and ``password`` must be mutable bytearrays.  Both are zeroed
    on every return path.  ``command`` and ``cwd`` exist for tests; production
    callers must accept their constant defaults.
    """

    if not isinstance(username, bytearray) or not isinstance(password, bytearray):
        raise TypeError("username and password must be bytearrays")

    observed = _Observed()
    scanner: Optional[_BoundedScanner] = None
    pid: Optional[int] = None
    master_fd: Optional[int] = None
    exit_code: Optional[int] = None
    forced_status: Optional[PtyStatus] = None
    termination_requested = False
    kill_required = False

    try:
        try:
            timeouts.validate()
            scanner = _BoundedScanner(max_scan_buffer)
        except ValueError:
            # Configuration errors are programmer errors, not runtime status.
            raise

        if not _credentials_valid(username, password):
            return _make_result(
                PtyStatus.INVALID_CREDENTIAL,
                None,
                observed,
                termination_requested=False,
                kill_required=False,
                scan_buffer_peak=0,
            )

        command_tuple = tuple(command)
        if (
            not command_tuple
            or not all(isinstance(value, str) for value in command_tuple)
            or not os.path.isabs(command_tuple[0])
            or not isinstance(cwd, str)
            or not os.path.isabs(cwd)
        ):
            raise ValueError("command executable and cwd must be absolute")

        if fcntl is None or termios is None or not hasattr(os, "fork"):
            return _make_result(
                PtyStatus.UNSUPPORTED_PLATFORM,
                None,
                observed,
                termination_requested=False,
                kill_required=False,
                scan_buffer_peak=0,
            )

        try:
            pid, master_fd = _spawn_pty(command_tuple, cwd)
        except OSError:
            return _make_result(
                PtyStatus.SPAWN_FAILED,
                None,
                observed,
                termination_requested=False,
                kill_required=False,
                scan_buffer_peak=scanner.peak,
            )

        started = time.monotonic()
        total_deadline = started + timeouts.total
        username_deadline = started + timeouts.username_prompt
        password_deadline: Optional[float] = None
        last_username_position = -1
        last_password_position = -1

        while exit_code is None and forced_status is None:
            now = time.monotonic()
            if not observed.username_prompt_seen and now >= username_deadline:
                forced_status = PtyStatus.USERNAME_PROMPT_TIMEOUT
                break
            if password_deadline is not None and now >= password_deadline:
                forced_status = PtyStatus.PASSWORD_PROMPT_TIMEOUT
                break
            if now >= total_deadline:
                forced_status = PtyStatus.TOTAL_TIMEOUT
                break

            nearest_deadline = total_deadline
            if not observed.username_prompt_seen:
                nearest_deadline = min(nearest_deadline, username_deadline)
            if password_deadline is not None:
                nearest_deadline = min(nearest_deadline, password_deadline)
            wait_for = min(
                timeouts.poll_interval,
                max(0.0, nearest_deadline - now),
            )

            try:
                readable, _, _ = select.select([master_fd], [], [], wait_for)
                if readable:
                    _read_once(master_fd, scanner)
            except OSError:
                forced_status = PtyStatus.IO_ERROR
                break

            # Update noninteractive markers before prompts so a sign-in marker
            # and password prompt arriving in one read are handled correctly.
            _refresh_markers(scanner, observed)
            if (
                observed.authenticated_session_detected
                and not observed.password_sent
            ):
                # The unchanged setup has explicitly skipped signin, so this
                # request's password will never be consumed.
                _zero_bytearray(password)

            if scanner.contains(_SUDO_PROMPT):
                forced_status = PtyStatus.UNEXPECTED_SUDO_PROMPT
                break
            if any(
                scanner.contains(token) for token in _UNSUPPORTED_AUTH_TOKENS
            ):
                forced_status = PtyStatus.UNSUPPORTED_AUTH_FLOW
                break

            username_positions = scanner.positions_after(
                _USERNAME_PROMPT, last_username_position
            )
            for position in username_positions:
                last_username_position = position
                if observed.username_prompt_seen:
                    forced_status = PtyStatus.REPEATED_USERNAME_PROMPT
                    break
                observed.username_prompt_seen = True
                try:
                    _write_line(
                        master_fd,
                        username,
                        time.monotonic() + timeouts.write,
                    )
                except (OSError, TimeoutError):
                    forced_status = PtyStatus.IO_ERROR
                    break
                finally:
                    # A partial write cannot be retried safely.  Discard the
                    # input before any potentially lengthy child cleanup.
                    _zero_bytearray(username)
                observed.username_sent = True
            if forced_status is not None:
                break

            if (
                observed.signin_started
                and not observed.password_sent
                and password_deadline is None
            ):
                password_deadline = time.monotonic() + timeouts.password_prompt

            password_positions = scanner.positions_after(
                _PASSWORD_PROMPT, last_password_position
            )
            for position in password_positions:
                last_password_position = position
                if observed.password_prompt_seen:
                    forced_status = PtyStatus.REPEATED_PASSWORD_PROMPT
                    break
                observed.password_prompt_seen = True
                if not observed.username_sent or not observed.signin_started:
                    forced_status = PtyStatus.UNEXPECTED_PASSWORD_PROMPT
                    break
                try:
                    _write_line(
                        master_fd,
                        password,
                        time.monotonic() + timeouts.write,
                    )
                except (OSError, TimeoutError):
                    forced_status = PtyStatus.IO_ERROR
                    break
                finally:
                    _zero_bytearray(password)
                observed.password_sent = True
                password_deadline = None
            if forced_status is not None:
                break

            exit_code = _poll_child(pid)

        if forced_status is not None and exit_code is None:
            termination_requested = True
            _zero_bytearray(username)
            _zero_bytearray(password)
            exit_code, kill_required = _terminate_process_group(
                pid,
                master_fd,
                scanner,
                timeouts,
                exit_code,
            )
        elif exit_code is not None:
            # Drain data already queued before child exit so final markers are
            # not lost merely because waitpid won a scheduling race.
            try:
                while True:
                    readable, _, _ = select.select([master_fd], [], [], 0)
                    if not readable:
                        break
                    if not _read_once(master_fd, scanner):
                        break
                    _refresh_markers(scanner, observed)
            except OSError:
                if forced_status is None:
                    forced_status = PtyStatus.IO_ERROR
            _refresh_markers(scanner, observed)

        if forced_status is not None:
            final_status = forced_status
        elif exit_code != 0:
            final_status = PtyStatus.COMMAND_FAILED
        elif all(
            getattr(observed, field) for field in _SUCCESS_TOKENS
        ):
            final_status = PtyStatus.SUCCESS
        else:
            final_status = PtyStatus.MISSING_SUCCESS_MARKER

        return _make_result(
            final_status,
            exit_code,
            observed,
            termination_requested=termination_requested,
            kill_required=kill_required,
            scan_buffer_peak=scanner.peak,
        )
    finally:
        _zero_bytearray(username)
        _zero_bytearray(password)
        if scanner is not None:
            scanner.zero()
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
        if pid is not None and exit_code is None:
            # Covers unexpected Python exceptions without leaving setup or a
            # credential-bearing child alive.
            try:
                _signal_group(pid, signal.SIGTERM)
                deadline = time.monotonic() + timeouts.terminate_grace
                while time.monotonic() < deadline:
                    polled = _poll_child(pid)
                    if polled is not None:
                        break
                    time.sleep(min(timeouts.poll_interval, 0.05))
                else:
                    _signal_group(pid, signal.SIGKILL)
                    try:
                        os.waitpid(pid, 0)
                    except ChildProcessError:
                        pass
            except BaseException:
                # Cleanup must not replace the original exception.
                pass
