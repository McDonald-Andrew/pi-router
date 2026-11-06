"""Durable, non-secret state records for the Pi Router image lifecycle.

The files handled here are data, never shell input.  Each record has an exact
line-oriented schema, and every mutating operation uses a file descriptor for
the already-validated state directory.  Production callers use the default
root-owned directory; tests may supply another absolute directory and owner.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import secrets
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Iterator, Mapping, Sequence


DEFAULT_STATE_DIR: Final = Path("/var/lib/pi-router-image")
FORMAT_VERSION: Final = "1"

VALID_STATES: Final = frozenset(
    {"new", "pre-reboot-complete", "post-reboot-complete", "ready"}
)
VALID_OPERATIONS: Final = frozenset(
    {"config", "pre-full", "pre-resume", "post", "final"}
)

STATUS_CODES: Final = frozenset(
    {
        "initializing-identity",
        "detecting-hardware",
        "waiting-lan-carrier",
        "waiting-wan-carrier",
        "waiting-wan-dhcp",
        "waiting-repository",
        "running-pre-reboot",
        "resuming-pre-reboot",
        "rebooting-after-pre",
        "waiting-desktop",
        "waiting-onboarding",
        "running-post-reboot",
        "rebooting-after-post",
        "waiting-final-services",
        "waiting-final-recovery",
        "running-final-verify",
        "ready",
        "error",
    }
)

# Error receipts intentionally contain only a bounded, non-secret enum.  Add a
# code here only when a controller can map it to a documented recovery action.
ERROR_CODES: Final = frozenset(
    {
        "invalid-state",
        "release-integrity",
        "invalid-config",
        "checkpoint-missing",
        "checkpoint-corrupt",
        "checkpoint-conflict",
        "resume-unsafe",
        "setup-pre-failed",
        "setup-post-failed",
        "setup-final-failed",
        "final-services-timeout",
        "forwarding-guard",
        "identity-generation-failed",
        "os-customization-failed",
        "hardware-mismatch",
        "lan-carrier-timeout",
        "wan-carrier-timeout",
        "wan-dhcp-timeout",
        "repository-unavailable",
        "authentication-failed",
        "credential-timeout",
        "unsupported-authentication",
        "internal-error",
    }
)

READY_PROOFS: Final = frozenset(
    {"exit-zero-and-marker", "checkpoint-consumed-after-durable-intent"}
)

STATE_FILENAME: Final = "state"
ATTEMPT_FILENAME: Final = "attempt"
STATUS_FILENAME: Final = "status"
ERROR_FILENAME: Final = "error"
READY_FILENAME: Final = "ready"
FINAL_WAIT_FILENAME: Final = "final-wait"

DIRECTORY_MODE: Final = 0o700
PRIVATE_FILE_MODE: Final = 0o600
STATUS_FILE_MODE: Final = 0o644
MAX_RECORD_BYTES: Final = 4096

FILE_MODES: Final[Mapping[str, int]] = MappingProxyType(
    {
        STATE_FILENAME: PRIVATE_FILE_MODE,
        ATTEMPT_FILENAME: PRIVATE_FILE_MODE,
        STATUS_FILENAME: STATUS_FILE_MODE,
        ERROR_FILENAME: PRIVATE_FILE_MODE,
        READY_FILENAME: PRIVATE_FILE_MODE,
        FINAL_WAIT_FILENAME: PRIVATE_FILE_MODE,
    }
)

_ATTEMPT_KEYS: Final = (
    "format",
    "operation",
    "invocation_boot_id",
    "checkpoint_before",
    "release_name",
    "release_setup_sha256",
    "late_apply_before",
    "original_pre_boot_id",
)
_STATUS_KEYS: Final = ("format", "state", "code")
_ERROR_KEYS: Final = ("format", "state", "code")
_READY_KEYS: Final = ("format", "release_name", "completed_boot_id", "proof")
_FINAL_WAIT_KEYS: Final = ("format", "boot_id", "checks_started")

_UUID_RE: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
_RELEASE_NAME_RE: Final = re.compile(
    r"pi-router-v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?"
)

_O_CLOEXEC: Final = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY: Final = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)


class ImageStateError(RuntimeError):
    """Base exception for image-state validation or persistence failures."""


class StateFormatError(ImageStateError):
    """A state value or serialized record is not in the canonical format."""


class StateSafetyError(ImageStateError):
    """A state path has unsafe type, ownership, links, or permissions."""


class StateDurabilityError(ImageStateError):
    """A required write, rename, removal, or fsync did not complete."""


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise StateFormatError(f"{field} must be a string")
    return value


def validate_state(value: str) -> str:
    value = _require_string(value, "state")
    if value not in VALID_STATES:
        raise StateFormatError(f"invalid image state: {value!r}")
    return value


def validate_boot_id(value: str, field: str = "boot ID") -> str:
    value = _require_string(value, field)
    if _UUID_RE.fullmatch(value) is None:
        raise StateFormatError(f"{field} is not a canonical lowercase UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise StateFormatError(f"{field} is not a valid UUID") from exc
    if str(parsed) != value or parsed.int == 0:
        raise StateFormatError(f"{field} is not a nonzero canonical UUID")
    return value


def validate_sha256(value: str) -> str:
    value = _require_string(value, "release setup SHA-256")
    if _SHA256_RE.fullmatch(value) is None:
        raise StateFormatError("release setup SHA-256 must be 64 lowercase hex digits")
    return value


def validate_release_name(value: str) -> str:
    value = _require_string(value, "release name")
    if _RELEASE_NAME_RE.fullmatch(value) is None:
        raise StateFormatError("release name must be pi-router-vX.Y.Z[-prerelease]")
    return value


def _validate_marker_or_boot_id(
    value: str,
    field: str,
    markers: frozenset[str],
) -> str:
    value = _require_string(value, field)
    if value in markers:
        return value
    return validate_boot_id(value, field)


@dataclass(frozen=True, slots=True)
class Attempt:
    """Durable intent written before a state-changing setup invocation."""

    operation: str
    invocation_boot_id: str
    checkpoint_before: str
    release_name: str
    release_setup_sha256: str
    late_apply_before: str
    original_pre_boot_id: str

    def __post_init__(self) -> None:
        operation = _require_string(self.operation, "operation")
        if operation not in VALID_OPERATIONS:
            raise StateFormatError(f"invalid attempt operation: {operation!r}")

        validate_boot_id(self.invocation_boot_id, "invocation boot ID")
        _validate_marker_or_boot_id(
            self.checkpoint_before, "checkpoint before", frozenset({"absent"})
        )
        validate_release_name(self.release_name)
        validate_sha256(self.release_setup_sha256)
        _validate_marker_or_boot_id(
            self.late_apply_before,
            "late-apply indicator before",
            frozenset({"absent", "present", "not-applicable"}),
        )
        _validate_marker_or_boot_id(
            self.original_pre_boot_id,
            "original pre-reboot boot ID",
            frozenset({"absent", "not-applicable"}),
        )

        if operation in {"config", "pre-full"}:
            expected = ("absent", "absent", "absent")
            actual = (
                self.checkpoint_before,
                self.late_apply_before,
                self.original_pre_boot_id,
            )
            if actual != expected:
                raise StateFormatError(
                    f"{operation} requires absent checkpoint, late-apply, and original boot ID"
                )
        elif operation == "pre-resume":
            if self.checkpoint_before != "absent":
                raise StateFormatError("pre-resume requires an absent checkpoint")
            if self.late_apply_before != "present":
                raise StateFormatError(
                    "pre-resume requires a present late-apply indicator"
                )
            validate_boot_id(
                self.original_pre_boot_id, "pre-resume original pre-reboot boot ID"
            )
        else:
            validate_boot_id(self.checkpoint_before, f"{operation} checkpoint before")
            if (
                self.late_apply_before != "not-applicable"
                or self.original_pre_boot_id != "not-applicable"
            ):
                raise StateFormatError(
                    f"{operation} requires not-applicable pre-reboot recovery fields"
                )


@dataclass(frozen=True, slots=True)
class Status:
    state: str
    code: str

    def __post_init__(self) -> None:
        validate_state(self.state)
        code = _require_string(self.code, "status code")
        if code not in STATUS_CODES:
            raise StateFormatError(f"invalid status code: {code!r}")


@dataclass(frozen=True, slots=True)
class ErrorReceipt:
    state: str
    code: str

    def __post_init__(self) -> None:
        validate_state(self.state)
        code = _require_string(self.code, "error code")
        if code not in ERROR_CODES:
            raise StateFormatError(f"invalid error code: {code!r}")


@dataclass(frozen=True, slots=True)
class ReadyReceipt:
    release_name: str
    completed_boot_id: str
    proof: str

    def __post_init__(self) -> None:
        validate_release_name(self.release_name)
        validate_boot_id(self.completed_boot_id, "completed boot ID")
        proof = _require_string(self.proof, "ready proof")
        if proof not in READY_PROOFS:
            raise StateFormatError(f"invalid ready proof: {proof!r}")


@dataclass(frozen=True, slots=True)
class FinalServiceWait:
    """Durable count of final-service readiness checks started for one boot."""

    boot_id: str
    checks_started: int

    def __post_init__(self) -> None:
        validate_boot_id(self.boot_id, "final-service wait boot ID")
        if (
            isinstance(self.checks_started, bool)
            or not isinstance(self.checks_started, int)
            or not 1 <= self.checks_started <= 999_999
        ):
            raise StateFormatError(
                "final-service check count must be an integer from 1 to 999999"
            )


def _parse_fields(text: str, keys: Sequence[str], record_name: str) -> dict[str, str]:
    text = _require_string(text, record_name)
    try:
        encoded = text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise StateFormatError(f"{record_name} must contain ASCII only") from exc
    if len(encoded) > MAX_RECORD_BYTES:
        raise StateFormatError(f"{record_name} exceeds {MAX_RECORD_BYTES} bytes")
    if "\r" in text or not text.endswith("\n") or text.endswith("\n\n"):
        raise StateFormatError(
            f"{record_name} must use one LF after each nonempty record line"
        )

    lines = text[:-1].split("\n")
    if len(lines) != len(keys):
        raise StateFormatError(
            f"{record_name} must contain exactly {len(keys)} ordered fields"
        )

    fields: dict[str, str] = {}
    for expected_key, line in zip(keys, lines):
        key, separator, value = line.partition("=")
        if separator != "=" or key != expected_key or not value:
            raise StateFormatError(
                f"{record_name} expected nonempty field {expected_key!r}"
            )
        fields[key] = value
    if fields.get("format") != FORMAT_VERSION:
        raise StateFormatError(
            f"{record_name} format must be {FORMAT_VERSION!r}"
        )
    return fields


def _format_fields(fields: Sequence[tuple[str, str]]) -> str:
    return "".join(f"{key}={value}\n" for key, value in fields)


def parse_state(text: str) -> str:
    text = _require_string(text, "state record")
    try:
        text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise StateFormatError("state record must contain ASCII only") from exc
    if len(text) > MAX_RECORD_BYTES or not text.endswith("\n"):
        raise StateFormatError("state record must be one LF-terminated state")
    if text.count("\n") != 1 or "\r" in text:
        raise StateFormatError("state record must contain exactly one line")
    return validate_state(text[:-1])


def format_state(state: str) -> str:
    return f"{validate_state(state)}\n"


def parse_attempt(text: str) -> Attempt:
    fields = _parse_fields(text, _ATTEMPT_KEYS, "attempt record")
    return Attempt(
        operation=fields["operation"],
        invocation_boot_id=fields["invocation_boot_id"],
        checkpoint_before=fields["checkpoint_before"],
        release_name=fields["release_name"],
        release_setup_sha256=fields["release_setup_sha256"],
        late_apply_before=fields["late_apply_before"],
        original_pre_boot_id=fields["original_pre_boot_id"],
    )


def format_attempt(attempt: Attempt) -> str:
    if not isinstance(attempt, Attempt):
        raise StateFormatError("attempt must be an Attempt instance")
    # Re-run validation even if a caller bypassed normal dataclass construction.
    Attempt(**{field: getattr(attempt, field) for field in Attempt.__dataclass_fields__})
    return _format_fields(
        (
            ("format", FORMAT_VERSION),
            ("operation", attempt.operation),
            ("invocation_boot_id", attempt.invocation_boot_id),
            ("checkpoint_before", attempt.checkpoint_before),
            ("release_name", attempt.release_name),
            ("release_setup_sha256", attempt.release_setup_sha256),
            ("late_apply_before", attempt.late_apply_before),
            ("original_pre_boot_id", attempt.original_pre_boot_id),
        )
    )


def parse_status(text: str) -> Status:
    fields = _parse_fields(text, _STATUS_KEYS, "status record")
    return Status(state=fields["state"], code=fields["code"])


def format_status(status: Status) -> str:
    if not isinstance(status, Status):
        raise StateFormatError("status must be a Status instance")
    Status(status.state, status.code)
    return _format_fields(
        (("format", FORMAT_VERSION), ("state", status.state), ("code", status.code))
    )


def parse_error(text: str) -> ErrorReceipt:
    fields = _parse_fields(text, _ERROR_KEYS, "error record")
    return ErrorReceipt(state=fields["state"], code=fields["code"])


def format_error(error: ErrorReceipt) -> str:
    if not isinstance(error, ErrorReceipt):
        raise StateFormatError("error must be an ErrorReceipt instance")
    ErrorReceipt(error.state, error.code)
    return _format_fields(
        (("format", FORMAT_VERSION), ("state", error.state), ("code", error.code))
    )


def parse_ready(text: str) -> ReadyReceipt:
    fields = _parse_fields(text, _READY_KEYS, "ready record")
    return ReadyReceipt(
        release_name=fields["release_name"],
        completed_boot_id=fields["completed_boot_id"],
        proof=fields["proof"],
    )


def format_ready(receipt: ReadyReceipt) -> str:
    if not isinstance(receipt, ReadyReceipt):
        raise StateFormatError("ready receipt must be a ReadyReceipt instance")
    ReadyReceipt(receipt.release_name, receipt.completed_boot_id, receipt.proof)
    return _format_fields(
        (
            ("format", FORMAT_VERSION),
            ("release_name", receipt.release_name),
            ("completed_boot_id", receipt.completed_boot_id),
            ("proof", receipt.proof),
        )
    )


def parse_final_wait(text: str) -> FinalServiceWait:
    fields = _parse_fields(text, _FINAL_WAIT_KEYS, "final-service wait record")
    counter = fields["checks_started"]
    if re.fullmatch(r"[1-9][0-9]{0,5}", counter) is None:
        raise StateFormatError("final-service check count must be canonical decimal")
    return FinalServiceWait(
        boot_id=fields["boot_id"],
        checks_started=int(counter),
    )


def format_final_wait(wait: FinalServiceWait) -> str:
    if not isinstance(wait, FinalServiceWait):
        raise StateFormatError("final-service wait must be a FinalServiceWait instance")
    FinalServiceWait(wait.boot_id, wait.checks_started)
    return _format_fields(
        (
            ("format", FORMAT_VERSION),
            ("boot_id", wait.boot_id),
            ("checks_started", str(wait.checks_started)),
        )
    )


class ImageStateStore:
    """Strict and durable storage rooted at one protected directory."""

    def __init__(
        self,
        state_dir: os.PathLike[str] | str = DEFAULT_STATE_DIR,
        *,
        expected_uid: int | None = 0,
        expected_gid: int | None = 0,
    ) -> None:
        supplied = Path(state_dir)
        if not supplied.is_absolute():
            raise ValueError("state_dir must be absolute")
        # abspath is lexical here; unlike resolve(), it does not follow a
        # possibly hostile final symlink before O_NOFOLLOW validation.
        self.state_dir = Path(os.path.abspath(os.fspath(supplied)))
        for value, label in ((expected_uid, "expected_uid"), (expected_gid, "expected_gid")):
            if value is not None and (not isinstance(value, int) or value < 0):
                raise ValueError(f"{label} must be a nonnegative integer or None")
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid

    def _validate_owner(self, metadata: os.stat_result, path_label: str) -> None:
        if self.expected_uid is not None and metadata.st_uid != self.expected_uid:
            raise StateSafetyError(
                f"{path_label} owner UID is {metadata.st_uid}, expected {self.expected_uid}"
            )
        if self.expected_gid is not None and metadata.st_gid != self.expected_gid:
            raise StateSafetyError(
                f"{path_label} owner GID is {metadata.st_gid}, expected {self.expected_gid}"
            )

    def _validate_directory(self, metadata: os.stat_result) -> None:
        if not stat.S_ISDIR(metadata.st_mode):
            raise StateSafetyError(f"{self.state_dir} is not a directory")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode != DIRECTORY_MODE:
            raise StateSafetyError(
                f"{self.state_dir} mode is {mode:#o}, expected {DIRECTORY_MODE:#o}"
            )
        self._validate_owner(metadata, str(self.state_dir))

    def _validate_file(
        self,
        metadata: os.stat_result,
        name: str,
        expected_mode: int,
    ) -> None:
        path_label = str(self.state_dir / name)
        if not stat.S_ISREG(metadata.st_mode):
            raise StateSafetyError(f"{path_label} is not a regular file")
        if metadata.st_nlink != 1:
            raise StateSafetyError(f"{path_label} must have exactly one hard link")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode != expected_mode:
            raise StateSafetyError(
                f"{path_label} mode is {mode:#o}, expected {expected_mode:#o}"
            )
        self._validate_owner(metadata, path_label)

    @staticmethod
    def _fsync(descriptor: int, label: str) -> None:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise StateDurabilityError(f"failed to fsync {label}") from exc

    def _sync_parent_after_create(self) -> None:
        flags = os.O_RDONLY | _O_CLOEXEC | _O_DIRECTORY | _O_NOFOLLOW
        try:
            descriptor = os.open(os.fspath(self.state_dir.parent), flags)
        except OSError as exc:
            raise StateDurabilityError(
                f"failed to open state-directory parent {self.state_dir.parent}"
            ) from exc
        try:
            self._fsync(descriptor, str(self.state_dir.parent))
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def _directory_descriptor(self) -> Iterator[int]:
        created = False
        try:
            os.mkdir(self.state_dir, DIRECTORY_MODE)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise StateDurabilityError(
                f"failed to create state directory {self.state_dir}"
            ) from exc

        flags = os.O_RDONLY | _O_CLOEXEC | _O_DIRECTORY | _O_NOFOLLOW
        try:
            descriptor = os.open(os.fspath(self.state_dir), flags)
        except OSError as exc:
            raise StateSafetyError(
                f"failed to open state directory {self.state_dir} safely"
            ) from exc

        try:
            if created:
                try:
                    os.fchmod(descriptor, DIRECTORY_MODE)
                except OSError as exc:
                    raise StateDurabilityError(
                        f"failed to set mode on {self.state_dir}"
                    ) from exc
            self._validate_directory(os.fstat(descriptor))
            if created:
                self._fsync(descriptor, str(self.state_dir))
                self._sync_parent_after_create()
            yield descriptor
        finally:
            os.close(descriptor)

    def ensure_directory(self) -> None:
        with self._directory_descriptor():
            pass

    @staticmethod
    def _check_filename(name: str) -> int:
        if name not in FILE_MODES:
            raise StateSafetyError(f"unsupported image-state filename: {name!r}")
        return FILE_MODES[name]

    def _read(self, name: str) -> str | None:
        expected_mode = self._check_filename(name)
        with self._directory_descriptor() as directory:
            flags = os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW
            try:
                descriptor = os.open(name, flags, dir_fd=directory)
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise StateSafetyError(
                        f"refusing symlink at {self.state_dir / name}"
                    ) from exc
                raise

            try:
                metadata = os.fstat(descriptor)
                self._validate_file(metadata, name, expected_mode)
                if metadata.st_size > MAX_RECORD_BYTES:
                    raise StateFormatError(
                        f"{self.state_dir / name} exceeds {MAX_RECORD_BYTES} bytes"
                    )

                chunks: list[bytes] = []
                remaining = MAX_RECORD_BYTES + 1
                while remaining:
                    try:
                        chunk = os.read(descriptor, min(remaining, 1024))
                    except InterruptedError:
                        continue
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                payload = b"".join(chunks)
                if len(payload) > MAX_RECORD_BYTES:
                    raise StateFormatError(
                        f"{self.state_dir / name} exceeds {MAX_RECORD_BYTES} bytes"
                    )
                try:
                    return payload.decode("ascii")
                except UnicodeDecodeError as exc:
                    raise StateFormatError(
                        f"{self.state_dir / name} must contain ASCII only"
                    ) from exc
            finally:
                os.close(descriptor)

    def _validate_existing_destination(
        self,
        directory: int,
        name: str,
        expected_mode: int,
    ) -> None:
        try:
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        self._validate_file(metadata, name, expected_mode)

    def _atomic_write(self, name: str, text: str) -> None:
        expected_mode = self._check_filename(name)
        text = _require_string(text, f"{name} record")
        try:
            payload = text.encode("ascii")
        except UnicodeEncodeError as exc:
            raise StateFormatError(f"{name} record must contain ASCII only") from exc
        if len(payload) > MAX_RECORD_BYTES:
            raise StateFormatError(f"{name} record exceeds {MAX_RECORD_BYTES} bytes")

        with self._directory_descriptor() as directory:
            self._validate_existing_destination(directory, name, expected_mode)
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _O_CLOEXEC
                | _O_NOFOLLOW
            )
            descriptor = -1
            temporary = ""
            for _ in range(128):
                candidate = f".{name}.tmp-{os.getpid()}-{secrets.token_hex(16)}"
                try:
                    descriptor = os.open(
                        candidate,
                        flags,
                        PRIVATE_FILE_MODE,
                        dir_fd=directory,
                    )
                    temporary = candidate
                    break
                except FileExistsError:
                    continue
            if descriptor < 0:
                raise StateDurabilityError(f"could not allocate temporary {name} file")

            renamed = False
            try:
                try:
                    os.fchmod(descriptor, expected_mode)
                    self._validate_file(os.fstat(descriptor), temporary, expected_mode)
                    view = memoryview(payload)
                    while view:
                        try:
                            written = os.write(descriptor, view)
                        except InterruptedError:
                            continue
                        if written <= 0:
                            raise StateDurabilityError(
                                f"short write while persisting {name}"
                            )
                        view = view[written:]
                    self._fsync(descriptor, f"temporary {name} file")
                finally:
                    os.close(descriptor)
                    descriptor = -1

                # Reject a destination swapped after the first check.  The
                # directory is root-only, so this is defense in depth against
                # accidental local replacement, not a claim to out-race root.
                self._validate_existing_destination(directory, name, expected_mode)
                try:
                    os.replace(
                        temporary,
                        name,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                    )
                except OSError as exc:
                    raise StateDurabilityError(
                        f"failed to atomically replace {self.state_dir / name}"
                    ) from exc
                renamed = True
                self._fsync(directory, str(self.state_dir))
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if not renamed and temporary:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        # Preserve the original exception.  A random, private
                        # temp file is inert and will never be parsed as state.
                        pass

    def durable_remove(self, name: str) -> bool:
        expected_mode = self._check_filename(name)
        with self._directory_descriptor() as directory:
            try:
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return False
            self._validate_file(metadata, name, expected_mode)
            try:
                os.unlink(name, dir_fd=directory)
            except OSError as exc:
                raise StateDurabilityError(
                    f"failed to remove {self.state_dir / name}"
                ) from exc
            self._fsync(directory, str(self.state_dir))
            return True

    def read_state(self) -> str:
        text = self._read(STATE_FILENAME)
        return "new" if text is None else parse_state(text)

    def write_state(self, state: str) -> str:
        self._atomic_write(STATE_FILENAME, format_state(state))
        return state

    def read_attempt(self) -> Attempt | None:
        text = self._read(ATTEMPT_FILENAME)
        return None if text is None else parse_attempt(text)

    def write_attempt(self, attempt: Attempt) -> Attempt:
        self._atomic_write(ATTEMPT_FILENAME, format_attempt(attempt))
        return attempt

    def clear_attempt(self) -> bool:
        return self.durable_remove(ATTEMPT_FILENAME)

    def read_status(self) -> Status | None:
        text = self._read(STATUS_FILENAME)
        return None if text is None else parse_status(text)

    def write_status(self, state: str, code: str) -> Status:
        status = Status(state=state, code=code)
        self._atomic_write(STATUS_FILENAME, format_status(status))
        return status

    def clear_status(self) -> bool:
        return self.durable_remove(STATUS_FILENAME)

    def read_error(self) -> ErrorReceipt | None:
        text = self._read(ERROR_FILENAME)
        return None if text is None else parse_error(text)

    def write_error(self, state: str, code: str) -> ErrorReceipt:
        error = ErrorReceipt(state=state, code=code)
        self._atomic_write(ERROR_FILENAME, format_error(error))
        return error

    def clear_error(self) -> bool:
        return self.durable_remove(ERROR_FILENAME)

    def read_ready(self) -> ReadyReceipt | None:
        text = self._read(READY_FILENAME)
        return None if text is None else parse_ready(text)

    def write_ready(
        self,
        release_name: str,
        completed_boot_id: str,
        proof: str,
    ) -> ReadyReceipt:
        receipt = ReadyReceipt(
            release_name=release_name,
            completed_boot_id=completed_boot_id,
            proof=proof,
        )
        self._atomic_write(READY_FILENAME, format_ready(receipt))
        return receipt

    def clear_ready(self) -> bool:
        return self.durable_remove(READY_FILENAME)

    def read_final_wait(self) -> FinalServiceWait | None:
        text = self._read(FINAL_WAIT_FILENAME)
        return None if text is None else parse_final_wait(text)

    def write_final_wait(self, wait: FinalServiceWait) -> FinalServiceWait:
        self._atomic_write(FINAL_WAIT_FILENAME, format_final_wait(wait))
        return wait

    def clear_final_wait(self) -> bool:
        return self.durable_remove(FINAL_WAIT_FILENAME)


__all__ = [
    "ATTEMPT_FILENAME",
    "DEFAULT_STATE_DIR",
    "ERROR_CODES",
    "ERROR_FILENAME",
    "ErrorReceipt",
    "FINAL_WAIT_FILENAME",
    "FinalServiceWait",
    "FORMAT_VERSION",
    "ImageStateError",
    "ImageStateStore",
    "READY_FILENAME",
    "READY_PROOFS",
    "ReadyReceipt",
    "STATE_FILENAME",
    "STATUS_CODES",
    "STATUS_FILENAME",
    "StateDurabilityError",
    "StateFormatError",
    "StateSafetyError",
    "Status",
    "Attempt",
    "VALID_OPERATIONS",
    "VALID_STATES",
    "format_attempt",
    "format_error",
    "format_final_wait",
    "format_ready",
    "format_state",
    "format_status",
    "parse_attempt",
    "parse_error",
    "parse_final_wait",
    "parse_ready",
    "parse_state",
    "parse_status",
    "validate_boot_id",
    "validate_release_name",
    "validate_sha256",
    "validate_state",
]
