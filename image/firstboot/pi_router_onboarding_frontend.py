#!/usr/bin/python3
"""Temporary, credential-safe browser frontend for Pi Router onboarding.

This process is deliberately unprivileged.  It serves a very small HTTPS
application on the provisioning LAN and forwards one bounded credential frame
to the privileged first-boot broker over an AF_UNIX SOCK_SEQPACKET socket.  It
never executes setup, changes durable state, or writes credentials to disk.
"""

from __future__ import annotations

import argparse
import collections
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import stat
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Callable, Final, Mapping, Protocol


DEFAULT_CONFIG_PATH: Final = Path("/etc/pi-router.conf")
DEFAULT_ASSET_DIRECTORY: Final = Path("/usr/local/share/pi-router-onboarding")
DEFAULT_SOCKET_PATH: Final = Path("/run/pi-router-image/onboarding.sock")
DEFAULT_PORT: Final = 8443

CREDENTIAL_VERSION: Final = 1
MAX_USERNAME_BYTES: Final = 254
MAX_PASSWORD_BYTES: Final = 1024
CREDENTIAL_HEADER_BYTES: Final = 5
MAX_CREDENTIAL_FRAME_BYTES: Final = (
    CREDENTIAL_HEADER_BYTES + MAX_USERNAME_BYTES + MAX_PASSWORD_BYTES
)
CREDENTIAL_CONTENT_TYPE: Final = "application/vnd.pi-router.credentials"

STATUS_REQUEST: Final = b"STATUS\n"
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
STATUS_RESPONSES: Final = {
    (code + "\n").encode("ascii"): code for code in STATUS_CODES
}
SUBMIT_RESPONSES: Final = {
    b"accepted\n": "accepted",
    b"busy\n": "busy",
    b"not-ready\n": "not-ready",
    b"invalid\n": "invalid",
    b"failed\n": "failed",
}

SESSION_COOKIE: Final = "__Host-pi_router_onboarding"
SESSION_SECONDS: Final = 30 * 60
MAX_SESSIONS: Final = 64
RATE_WINDOW_SECONDS: Final = 15 * 60
RATE_CLIENT_LIMIT: Final = 5
RATE_GLOBAL_LIMIT: Final = 10
RATE_COOLDOWN_SECONDS: Final = 3
MAX_RATE_CLIENTS: Final = 64
MAX_HTTP_WORKERS: Final = 8
HTTP_TIMEOUT_SECONDS: Final = 10.0
IPC_TIMEOUT_SECONDS: Final = 5.0
STATUS_CACHE_SECONDS: Final = 1.0

_TOKEN_RE: Final = re.compile(r"\A[A-Za-z0-9_-]{43}\Z")
_CONFIG_RE: Final = re.compile(
    r'''\ALAN_ADDR=(?:"([0-9.]+)"|'([0-9.]+)'|([0-9.]+))\Z'''
)

_CSP: Final = (
    "default-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


class FrontendError(Exception):
    """A non-secret frontend failure safe to collapse to a fixed response."""


class InvalidCredentialFrame(FrontendError):
    """The submitted binary credential frame failed strict validation."""


class BackendUnavailable(FrontendError):
    """The privileged broker was unavailable or violated its protocol."""


class Backend(Protocol):
    def status(self) -> str:
        """Return one fixed status code."""

    def submit(self, frame: bytearray) -> str:
        """Submit one already-validated mutable credential frame."""


def zero_bytearray(value: bytearray) -> None:
    """Best-effort overwrite of a mutable credential buffer."""

    value[:] = b"\x00" * len(value)


def _valid_utf8(value: memoryview) -> bool:
    """Validate UTF-8 directly over a mutable view, without making a copy."""

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


def validate_credential_frame(frame: bytearray) -> tuple[int, int]:
    """Validate the fixed credential envelope without retaining credentials.

    The frame is ``version:u8, username_length:u16be,
    password_length:u16be, username:utf8, password:utf8``.  Only the two
    lengths are returned; callers continue to own and eventually overwrite the
    original bytearray.
    """

    if not isinstance(frame, bytearray):
        raise InvalidCredentialFrame()
    if not CREDENTIAL_HEADER_BYTES <= len(frame) <= MAX_CREDENTIAL_FRAME_BYTES:
        raise InvalidCredentialFrame()
    if frame[0] != CREDENTIAL_VERSION:
        raise InvalidCredentialFrame()

    username_length = (frame[1] << 8) | frame[2]
    password_length = (frame[3] << 8) | frame[4]
    if not 1 <= username_length <= MAX_USERNAME_BYTES:
        raise InvalidCredentialFrame()
    if not 1 <= password_length <= MAX_PASSWORD_BYTES:
        raise InvalidCredentialFrame()
    if len(frame) != CREDENTIAL_HEADER_BYTES + username_length + password_length:
        raise InvalidCredentialFrame()

    username_start = CREDENTIAL_HEADER_BYTES
    password_start = username_start + username_length
    username = memoryview(frame)[username_start:password_start]
    password = memoryview(frame)[password_start:]
    try:
        for part in (username, password):
            # The credentials cross a terminal after this boundary. Reject
            # every ASCII control byte (including ISIG/EOF characters), not
            # only line delimiters, before the PTY can interpret it.
            if any(byte < 0x20 or byte == 0x7F for byte in part):
                raise InvalidCredentialFrame()
            if not _valid_utf8(part):
                raise InvalidCredentialFrame()
    finally:
        username.release()
        password.release()
    return username_length, password_length


def load_lan_address(
    config_path: Path,
    *,
    expected_owner: int | None = 0,
    maximum_bytes: int = 16 * 1024,
) -> str:
    """Read exactly one literal LAN_ADDR assignment from trusted config."""

    try:
        metadata = config_path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise FrontendError()
        if expected_owner is not None and metadata.st_uid != expected_owner:
            raise FrontendError()
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise FrontendError()
        if metadata.st_size > maximum_bytes:
            raise FrontendError()
        raw = config_path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise FrontendError() from exc

    values: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("LAN_ADDR="):
            continue
        match = _CONFIG_RE.fullmatch(line)
        if match is None:
            raise FrontendError()
        values.append(next(value for value in match.groups() if value is not None))
    if len(values) != 1:
        raise FrontendError()

    try:
        address = ipaddress.IPv4Address(values[0])
    except ipaddress.AddressValueError as exc:
        raise FrontendError() from exc
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        raise FrontendError()
    return str(address)


@dataclass(frozen=True)
class AssetBundle:
    index: bytes
    javascript: bytes
    stylesheet: bytes
    favicon: bytes

    @classmethod
    def load(cls, directory: Path) -> "AssetBundle":
        try:
            index = (directory / "index.html").read_bytes()
            javascript = (directory / "app.js").read_bytes()
            stylesheet = (directory / "styles.css").read_bytes()
            favicon = (directory / "favicon.svg").read_bytes()
        except OSError as exc:
            raise FrontendError() from exc
        if b"__PI_ROUTER_CSRF_TOKEN__" not in index:
            raise FrontendError()
        if (
            max(len(index), len(javascript), len(stylesheet), len(favicon))
            > 256 * 1024
        ):
            raise FrontendError()
        return cls(
            index=index,
            javascript=javascript,
            stylesheet=stylesheet,
            favicon=favicon,
        )

    def index_for(self, csrf_token: str) -> bytes:
        if _TOKEN_RE.fullmatch(csrf_token) is None:
            raise FrontendError()
        return self.index.replace(
            b"__PI_ROUTER_CSRF_TOKEN__", csrf_token.encode("ascii")
        )


class BrokerClient:
    """Strict, one-packet client for the privileged onboarding broker."""

    def __init__(
        self,
        socket_path: Path = DEFAULT_SOCKET_PATH,
        *,
        timeout: float = IPC_TIMEOUT_SECONDS,
        socket_factory: Callable[..., socket.socket] = socket.socket,
    ) -> None:
        if not socket_path.is_absolute():
            raise ValueError("broker socket path must be absolute")
        self._socket_path = socket_path
        self._timeout = timeout
        self._socket_factory = socket_factory

    def _exchange(self, request: bytes | bytearray) -> bytes:
        client: socket.socket | None = None
        try:
            client = self._socket_factory(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            client.settimeout(self._timeout)
            client.connect(str(self._socket_path))
            sent = client.send(memoryview(request))
            if sent != len(request):
                raise BackendUnavailable()
            response, _ancillary, flags, _address = client.recvmsg(128)
            if flags & getattr(socket, "MSG_TRUNC", 0):
                raise BackendUnavailable()
            return response
        except (OSError, TimeoutError) as exc:
            raise BackendUnavailable() from exc
        finally:
            if client is not None:
                try:
                    client.close()
                except OSError:
                    pass

    def status(self) -> str:
        response = self._exchange(STATUS_REQUEST)
        try:
            return STATUS_RESPONSES[response]
        except KeyError as exc:
            raise BackendUnavailable() from exc

    def submit(self, frame: bytearray) -> str:
        validate_credential_frame(frame)
        response = self._exchange(frame)
        try:
            return SUBMIT_RESPONSES[response]
        except KeyError as exc:
            raise BackendUnavailable() from exc


@dataclass
class _Session:
    csrf_token: str
    expires_at: float


class SessionStore:
    """Bounded, memory-only sessions and first-valid-session claim."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[int], str] = secrets.token_urlsafe,
        lifetime: float = SESSION_SECONDS,
        maximum: int = MAX_SESSIONS,
    ) -> None:
        self._clock = clock
        self._token_factory = token_factory
        self._lifetime = lifetime
        self._maximum = maximum
        self._sessions: collections.OrderedDict[str, _Session] = (
            collections.OrderedDict()
        )
        self._claimant: str | None = None
        self._lock = threading.Lock()

    def _new_token(self) -> str:
        token = self._token_factory(32)
        if _TOKEN_RE.fullmatch(token) is None:
            raise FrontendError()
        return token

    def _prune(self, now: float) -> None:
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)
            if self._claimant == session_id:
                self._claimant = None

    def create(self) -> tuple[str, str] | None:
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._sessions) >= self._maximum:
                # Never evict the session that has claimed onboarding.  Old
                # unclaimed browser tabs are safe to replace, which prevents
                # a burst of GET / requests from permanently exhausting the
                # bounded in-memory store on the trusted provisioning LAN.
                evictable = next(
                    (
                        session_id
                        for session_id in self._sessions
                        if session_id != self._claimant
                    ),
                    None,
                )
                if evictable is None:
                    return None
                self._sessions.pop(evictable, None)
            session_id = self._new_token()
            csrf_token = self._new_token()
            while session_id in self._sessions:
                session_id = self._new_token()
            self._sessions[session_id] = _Session(
                csrf_token=csrf_token, expires_at=now + self._lifetime
            )
            return session_id, csrf_token

    def csrf_for(self, session_id: str) -> str | None:
        if _TOKEN_RE.fullmatch(session_id) is None:
            return None
        with self._lock:
            now = self._clock()
            self._prune(now)
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.expires_at = now + self._lifetime
            self._sessions.move_to_end(session_id)
            return session.csrf_token

    def verify_csrf(self, session_id: str, supplied: str) -> bool:
        expected = self.csrf_for(session_id)
        return expected is not None and hmac.compare_digest(expected, supplied)

    def claim(self, session_id: str) -> bool:
        with self._lock:
            now = self._clock()
            self._prune(now)
            if session_id not in self._sessions:
                return False
            if self._claimant is None:
                self._claimant = session_id
            session = self._sessions[session_id]
            session.expires_at = now + self._lifetime
            self._sessions.move_to_end(session_id)
            return hmac.compare_digest(self._claimant, session_id)


class RateLimiter:
    """Bounded per-client and global sliding-window limiter."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        window: float = RATE_WINDOW_SECONDS,
        client_limit: int = RATE_CLIENT_LIMIT,
        global_limit: int = RATE_GLOBAL_LIMIT,
        cooldown: float = RATE_COOLDOWN_SECONDS,
        maximum_clients: int = MAX_RATE_CLIENTS,
    ) -> None:
        self._clock = clock
        self._window = window
        self._client_limit = client_limit
        self._global_limit = global_limit
        self._cooldown = cooldown
        self._maximum_clients = maximum_clients
        self._global: collections.deque[float] = collections.deque()
        self._clients: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _trim(values: collections.deque[float], cutoff: float) -> None:
        while values and values[0] <= cutoff:
            values.popleft()

    def allow(self, client: str) -> tuple[bool, int]:
        now = self._clock()
        with self._lock:
            cutoff = now - self._window
            self._trim(self._global, cutoff)
            for key in tuple(self._clients):
                self._trim(self._clients[key], cutoff)
                if not self._clients[key]:
                    del self._clients[key]

            values = self._clients.get(client)
            if values is None:
                if len(self._clients) >= self._maximum_clients:
                    return False, int(self._window)
                values = collections.deque()
                self._clients[client] = values

            if values and now - values[-1] < self._cooldown:
                retry = max(1, int(self._cooldown - (now - values[-1]) + 0.999))
                return False, retry
            if len(values) >= self._client_limit or len(self._global) >= self._global_limit:
                oldest = values[0] if len(values) >= self._client_limit else self._global[0]
                retry = max(1, int(oldest + self._window - now + 0.999))
                return False, retry

            values.append(now)
            self._global.append(now)
            return True, 0


_STATUS_SUMMARIES: Final[Mapping[str, str]] = {
    "initializing-identity": "Initializing this router",
    "detecting-hardware": "Checking router hardware",
    "waiting-lan-carrier": "Connect the downstream LAN cable",
    "waiting-wan-carrier": "Connect the Waveshare WAN cable",
    "waiting-wan-dhcp": "Waiting for an Internet address",
    "waiting-repository": "Waiting for the certified release",
    "running-pre-reboot": "Installing router services",
    "resuming-pre-reboot": "Safely resuming installation",
    "rebooting-after-pre": "Router is restarting",
    "waiting-desktop": "Waiting for the local desktop session",
    "waiting-onboarding": "Ready for Proton sign-in",
    "running-post-reboot": "Signing in and verifying Proton",
    "rebooting-after-post": "Proton verified; router is restarting",
    "waiting-final-services": "Waiting for router services",
    "waiting-final-recovery": "Confirming final verification commit",
    "running-final-verify": "Running final verification",
    "ready": "Pi Router is ready",
    "error": "Setup needs attention",
}


@dataclass(frozen=True)
class FrontendSettings:
    lan_address: str
    port: int = DEFAULT_PORT
    provisioning_network: str = "192.168.50.0/24"

    def __post_init__(self) -> None:
        address = ipaddress.IPv4Address(self.lan_address)
        network = ipaddress.IPv4Network(self.provisioning_network, strict=False)
        if address not in network:
            raise ValueError("LAN address must be inside provisioning network")
        if not 1 <= self.port <= 65535:
            raise ValueError("invalid port")

    @property
    def authority(self) -> str:
        return f"{self.lan_address}:{self.port}"

    @property
    def origin(self) -> str:
        return f"https://{self.authority}"


class FrontendApplication:
    def __init__(
        self,
        settings: FrontendSettings,
        assets: AssetBundle,
        backend: Backend,
        *,
        sessions: SessionStore | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.settings = settings
        self.assets = assets
        self.backend = backend
        self.sessions = sessions if sessions is not None else SessionStore()
        self.rate_limiter = (
            rate_limiter if rate_limiter is not None else RateLimiter()
        )
        self.provisioning_network = ipaddress.IPv4Network(
            settings.provisioning_network, strict=False
        )
        self.attempt_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._status_cached_until = 0.0
        self._status_cached: tuple[int, bytes] | None = None

    def status_document(self) -> tuple[int, bytes]:
        # Collapse concurrent browser polling to at most one broker exchange
        # per cache interval.  This is a load bound, not an authorization
        # boundary; every HTTP request still needs a live session.
        with self._status_lock:
            now = time.monotonic()
            if self._status_cached is not None and now < self._status_cached_until:
                return self._status_cached
            try:
                code = self.backend.status()
                document = {"status": code, "summary": _STATUS_SUMMARIES[code]}
                response = (
                    200,
                    json.dumps(document, separators=(",", ":")).encode("ascii"),
                )
            except (BackendUnavailable, KeyError):
                document = {
                    "status": "unavailable",
                    "summary": "Onboarding service is starting",
                }
                response = (
                    503,
                    json.dumps(document, separators=(",", ":")).encode("ascii"),
                )
            self._status_cached = response
            self._status_cached_until = now + STATUS_CACHE_SECONDS
            return response


class BoundedThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = True
    request_queue_size = 16
    allow_reuse_address = False

    def __init__(
        self,
        server_address: tuple[str, int],
        application: FrontendApplication,
        *,
        maximum_workers: int = MAX_HTTP_WORKERS,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self.application = application
        self._workers = threading.BoundedSemaphore(maximum_workers)
        self._tls_context = tls_context
        super().__init__(server_address, OnboardingRequestHandler)

    def process_request(self, request: socket.socket, client_address: object) -> None:
        if not self._workers.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._workers.release()
            raise

    def _tls_request(self, request: socket.socket) -> socket.socket:
        if self._tls_context is None:
            return request
        # The timeout is set on the accepted TCP socket before any TLS work.
        # Handshake-on-connect is disabled so the single accept loop can never
        # be held by a client that opens TCP and then stalls negotiation.
        request.settimeout(HTTP_TIMEOUT_SECONDS)
        secured = self._tls_context.wrap_socket(
            request,
            server_side=True,
            do_handshake_on_connect=False,
        )
        try:
            secured.settimeout(HTTP_TIMEOUT_SECONDS)
            secured.do_handshake()
        except BaseException:
            secured.close()
            raise
        return secured

    def process_request_thread(self, request: socket.socket, client_address: object) -> None:
        active_request = request
        dispatched = False
        try:
            active_request = self._tls_request(request)
            dispatched = True
            super().process_request_thread(active_request, client_address)
        except (OSError, TimeoutError, ssl.SSLError):
            if not dispatched:
                try:
                    self.shutdown_request(active_request)
                except OSError:
                    pass
        finally:
            self._workers.release()

    def handle_error(self, request: object, client_address: object) -> None:
        # BaseServer normally prints an exception and request metadata.  The
        # appliance intentionally emits no per-request or exception log.
        return


class OnboardingRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PiRouterOnboarding"
    sys_version = ""

    @property
    def application(self) -> FrontendApplication:
        return self.server.application  # type: ignore[attr-defined,no-any-return]

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(HTTP_TIMEOUT_SECONDS)

    def log_message(self, format: str, *args: object) -> None:
        return

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        return

    def log_error(self, format: str, *args: object) -> None:
        return

    def handle_expect_100(self) -> bool:
        self._respond_json(417, {"error": "request-rejected"})
        return False

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        self._respond_json(code, {"error": "request-rejected"})

    def _security_headers(self, content_type: str, content_length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store, no-cache, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Connection", "close")
        self.close_connection = True

    def _respond(
        self,
        code: int,
        body: bytes,
        content_type: str,
        *,
        cookie: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.send_response(code)
        self._security_headers(content_type, len(body))
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionError, OSError):
                pass

    def _respond_json(
        self,
        code: int,
        document: Mapping[str, object],
        *,
        cookie: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        body = json.dumps(document, separators=(",", ":")).encode("ascii")
        self._respond(
            code,
            body,
            "application/json; charset=utf-8",
            cookie=cookie,
            retry_after=retry_after,
        )

    def _single_header(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        if len(values) != 1:
            return None
        return values[0]

    def _valid_common_request(self) -> bool:
        if self.request_version != "HTTP/1.1":
            self._respond_json(400, {"error": "invalid-request"})
            return False
        if (
            not self.path.startswith("/")
            or self.path.startswith("//")
            or "?" in self.path
            or "#" in self.path
        ):
            self._respond_json(400, {"error": "invalid-target"})
            return False
        if self._single_header("Host") != self.application.settings.authority:
            self._respond_json(400, {"error": "invalid-host"})
            return False
        if self.headers.get_all("Transfer-Encoding", []):
            self._respond_json(400, {"error": "invalid-framing"})
            return False
        try:
            remote = ipaddress.IPv4Address(self.client_address[0])
        except ipaddress.AddressValueError:
            self._respond_json(403, {"error": "provisioning-lan-only"})
            return False
        if remote not in self.application.provisioning_network:
            self._respond_json(403, {"error": "provisioning-lan-only"})
            return False
        return True

    def _session_id(self) -> str | None:
        header = self._single_header("Cookie")
        if header is None or len(header) > 4096:
            return None
        found: str | None = None
        for part in header.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == SESSION_COOKIE:
                if found is not None or _TOKEN_RE.fullmatch(value) is None:
                    return None
                found = value
        return found

    def _require_session(self) -> str | None:
        session_id = self._session_id()
        if session_id is None or self.application.sessions.csrf_for(session_id) is None:
            self._respond_json(401, {"error": "session-required"})
            return None
        return session_id

    @staticmethod
    def _session_cookie(session_id: str) -> str:
        if _TOKEN_RE.fullmatch(session_id) is None:
            raise FrontendError()
        return (
            f"{SESSION_COOKIE}={session_id}; Path=/; Max-Age={SESSION_SECONDS}; "
            "Secure; HttpOnly; SameSite=Strict"
        )

    def _read_credential_body(self) -> bytearray:
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None:
            raise InvalidCredentialFrame()
        length = int(lengths[0], 10)
        if not CREDENTIAL_HEADER_BYTES <= length <= MAX_CREDENTIAL_FRAME_BYTES:
            raise InvalidCredentialFrame()
        if self._single_header("Content-Type") != CREDENTIAL_CONTENT_TYPE:
            raise InvalidCredentialFrame()

        body = bytearray(length)
        view = memoryview(body)
        offset = 0
        try:
            while offset < length:
                read = self.rfile.readinto(view[offset:])
                if read is None or read <= 0:
                    raise InvalidCredentialFrame()
                offset += read
            validate_credential_frame(body)
            return body
        except BaseException:
            zero_bytearray(body)
            raise
        finally:
            view.release()

    def do_GET(self) -> None:
        try:
            if not self._valid_common_request():
                return
            if self.headers.get_all("Content-Length", []):
                self._respond_json(400, {"error": "invalid-framing"})
                return
            if self.path == "/":
                session_id = self._session_id()
                csrf_token = (
                    self.application.sessions.csrf_for(session_id)
                    if session_id is not None
                    else None
                )
                if csrf_token is None:
                    created = self.application.sessions.create()
                    if created is None:
                        self._respond_json(
                            503, {"error": "temporarily-unavailable"}
                        )
                        return
                    session_id, csrf_token = created
                body = self.application.assets.index_for(csrf_token)
                cookie = self._session_cookie(session_id)
                self._respond(
                    200,
                    body,
                    "text/html; charset=utf-8",
                    cookie=cookie,
                )
                return
            if self.path == "/onboarding-app.js":
                self._respond(
                    200,
                    self.application.assets.javascript,
                    "application/javascript; charset=utf-8",
                )
                return
            if self.path == "/src/web/styles.css":
                self._respond(
                    200,
                    self.application.assets.stylesheet,
                    "text/css; charset=utf-8",
                )
                return
            if self.path == "/src/web/favicon.svg":
                self._respond(
                    200,
                    self.application.assets.favicon,
                    "image/svg+xml",
                )
                return
            if self.path == "/api/status":
                session_id = self._require_session()
                if session_id is None:
                    return
                code, document = self.application.status_document()
                self._respond(
                    code,
                    document,
                    "application/json; charset=utf-8",
                    cookie=self._session_cookie(session_id),
                )
                return
            self._respond_json(404, {"error": "not-found"})
        except BaseException:
            self._respond_json(500, {"error": "internal-error"})

    def do_POST(self) -> None:
        body: bytearray | None = None
        acquired = False
        cookie: str | None = None
        try:
            if not self._valid_common_request():
                return
            if self.path != "/api/onboard":
                self._respond_json(404, {"error": "not-found"})
                return
            if self._single_header("Origin") != self.application.settings.origin:
                self._respond_json(403, {"error": "invalid-origin"})
                return
            session_id = self._require_session()
            if session_id is None:
                return
            cookie = self._session_cookie(session_id)
            csrf = self._single_header("X-Pi-Router-CSRF")
            if csrf is None or not self.application.sessions.verify_csrf(
                session_id, csrf
            ):
                self._respond_json(403, {"error": "invalid-csrf"}, cookie=cookie)
                return

            allowed, retry_after = self.application.rate_limiter.allow(
                self.client_address[0]
            )
            if not allowed:
                self._respond_json(
                    429,
                    {"error": "rate-limited"},
                    cookie=cookie,
                    retry_after=retry_after,
                )
                return

            body = self._read_credential_body()
            if not self.application.sessions.claim(session_id):
                self._respond_json(
                    423, {"error": "onboarding-claimed"}, cookie=cookie
                )
                return
            acquired = self.application.attempt_lock.acquire(blocking=False)
            if not acquired:
                self._respond_json(
                    423, {"error": "attempt-in-progress"}, cookie=cookie
                )
                return

            result = self.application.backend.submit(body)
            if result == "accepted":
                self._respond_json(202, {"status": "accepted"}, cookie=cookie)
            elif result == "busy":
                self._respond_json(
                    423, {"error": "attempt-in-progress"}, cookie=cookie
                )
            elif result == "not-ready":
                self._respond_json(409, {"error": "not-ready"}, cookie=cookie)
            elif result == "invalid":
                self._respond_json(
                    400, {"error": "invalid-credentials"}, cookie=cookie
                )
            else:
                self._respond_json(503, {"error": "attempt-failed"}, cookie=cookie)
        except InvalidCredentialFrame:
            self._respond_json(400, {"error": "invalid-credentials"}, cookie=cookie)
        except BackendUnavailable:
            self._respond_json(503, {"error": "backend-unavailable"}, cookie=cookie)
        except (TimeoutError, OSError):
            self._respond_json(408, {"error": "request-timeout"}, cookie=cookie)
        except BaseException:
            self._respond_json(500, {"error": "internal-error"}, cookie=cookie)
        finally:
            if acquired:
                self.application.attempt_lock.release()
            if body is not None:
                zero_bytearray(body)

    def do_HEAD(self) -> None:
        self._respond_json(405, {"error": "method-not-allowed"})

    def do_OPTIONS(self) -> None:
        self._respond_json(405, {"error": "method-not-allowed"})

    do_PUT = do_OPTIONS
    do_PATCH = do_OPTIONS
    do_DELETE = do_OPTIONS
    do_CONNECT = do_OPTIONS
    do_TRACE = do_OPTIONS


def build_tls_context(certificate: Path, private_key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.options |= ssl.OP_NO_COMPRESSION
    try:
        context.load_cert_chain(str(certificate), str(private_key))
    except (OSError, ssl.SSLError) as exc:
        raise FrontendError() from exc
    return context


def create_https_server(
    application: FrontendApplication,
    tls_context: ssl.SSLContext,
) -> BoundedThreadingHTTPServer:
    return BoundedThreadingHTTPServer(
        (application.settings.lan_address, application.settings.port),
        application,
        tls_context=tls_context,
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--assets", type=Path, default=DEFAULT_ASSET_DIRECTORY)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _argument_parser().parse_args(argv)
        credential_directory_value = os.environ.get("CREDENTIALS_DIRECTORY", "")
        if not credential_directory_value:
            raise FrontendError()
        credential_directory = Path(credential_directory_value)
        if not credential_directory.is_absolute():
            raise FrontendError()

        lan_address = load_lan_address(arguments.config)
        settings = FrontendSettings(lan_address=lan_address, port=arguments.port)
        assets = AssetBundle.load(arguments.assets)
        backend = BrokerClient(arguments.socket)
        application = FrontendApplication(settings, assets, backend)
        tls_context = build_tls_context(
            credential_directory / "tls.crt", credential_directory / "tls.key"
        )
        server = create_https_server(application, tls_context)
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            server.server_close()
        return 0
    except (FrontendError, ValueError, OSError, ssl.SSLError):
        # Deliberately fixed: no paths, request data, or exception text reaches
        # the journal.  systemd reports the nonzero service result.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
