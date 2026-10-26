#!/usr/bin/env python3
"""Small LAN-only web UI/API for the Raspberry Pi Proton router."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import mimetypes
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

CONFIG_FILE = Path("/etc/pi-router.conf")
BYPASS_FILE = Path("/etc/proton-bypass/devices.conf")
PROTON_BYPASS = "/usr/local/sbin/proton-bypass"
IP_CMD = "/usr/sbin/ip"
SUDO_CMD = "/usr/bin/sudo"
SYSTEMCTL_CMD = "/usr/bin/systemctl"
UPDATE_CONFIG_FILE = Path(
    "/etc/pi-router-update.conf"
)
UPDATE_SERVICE = "pi-router-update.service"
UPDATE_REPOSITORY_PLACEHOLDER = (
    "https://github.com/OWNER/REPOSITORY"
)
GITHUB_API_VERSION = "2022-11-28"
MAX_RELEASE_RESPONSE = 1024 * 1024

IP_BLOCK_STATUS_FILE = Path(
    "/var/lib/pi-router/ip-block/current/status"
)

IP_REFRESH_SERVICE = (
    "pi-router-ip-refresh.service"
)

IP_REFRESH_TIMER = (
    "pi-router-ip-refresh.timer"
)

DEFAULT_STATIC_ROOT = Path("/usr/local/share/pi-router-web")
DEFAULT_PORT = 8080
DEFAULT_VERSION = "v0.0.1"
MAX_BODY = 4096

MAC_RE = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")
REPOSITORY_PATH_RE = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)
VERSION_RE = re.compile(
    r"^v?(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class ConfigError(RuntimeError):
    pass


def parse_version(
    value: str,
) -> tuple[
    tuple[int, int, int],
    tuple[str, ...] | None,
]:
    match = VERSION_RE.fullmatch(
        value.strip()
    )

    if not match:
        raise ValueError(
            f"invalid release version: {value}"
        )

    core = tuple(
        int(match.group(index))
        for index in range(1, 4)
    )

    prerelease = (
        tuple(match.group(4).split("."))
        if match.group(4)
        else None
    )

    return core, prerelease


def version_is_newer(
    candidate: str,
    current: str,
) -> bool:
    candidate_core, candidate_pre = (
        parse_version(candidate)
    )
    current_core, current_pre = (
        parse_version(current)
    )

    if candidate_core != current_core:
        return candidate_core > current_core

    if candidate_pre is None:
        return current_pre is not None

    if current_pre is None:
        return False

    for candidate_part, current_part in zip(
        candidate_pre,
        current_pre,
    ):
        if candidate_part == current_part:
            continue

        candidate_numeric = (
            candidate_part.isdigit()
        )
        current_numeric = current_part.isdigit()

        if candidate_numeric and current_numeric:
            return int(candidate_part) > int(
                current_part
            )

        if candidate_numeric != current_numeric:
            return not candidate_numeric

        return candidate_part > current_part

    return len(candidate_pre) > len(current_pre)


def update_repository() -> str:
    try:
        values = read_shell_config_values(
            UPDATE_CONFIG_FILE
        )
    except ConfigError:
        raise

    repository_url = values.get(
        "GITHUB_REPOSITORY_URL",
        "",
    )

    if (
        not repository_url
        or repository_url
        == UPDATE_REPOSITORY_PLACEHOLDER
    ):
        raise ConfigError(
            "Update repository is not configured."
        )

    try:
        parsed = urlsplit(repository_url)
    except ValueError as exc:
        raise ConfigError(
            "Update repository URL is invalid."
        ) from exc

    repository = parsed.path.strip("/")

    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
        or not REPOSITORY_PATH_RE.fullmatch(
            repository
        )
    ):
        raise ConfigError(
            "Update repository must be an https://github.com/OWNER/REPOSITORY URL."
        )

    return repository


def latest_release(
    current_version: str,
) -> dict:
    repository = update_repository()
    request = Request(
        "https://api.github.com/repos/"
        f"{repository}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "pi-router-updater",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )

    try:
        with urlopen(
            request,
            timeout=10,
        ) as response:
            body = response.read(
                MAX_RELEASE_RESPONSE + 1
            )
    except HTTPError as exc:
        raise RuntimeError(
            "GitHub release check failed "
            f"(HTTP {exc.code})."
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            "Unable to reach GitHub for the release check."
        ) from exc

    if len(body) > MAX_RELEASE_RESPONSE:
        raise RuntimeError(
            "GitHub release response was too large."
        )

    try:
        payload = json.loads(
            body.decode("utf-8")
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError(
            "GitHub returned an invalid release response."
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError(
            "GitHub returned an invalid release response."
        )

    tag = str(
        payload.get("tag_name", "")
    ).strip()

    try:
        parse_version(tag)
        update_available = version_is_newer(
            tag,
            current_version,
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    normalized_tag = (
        tag if tag.startswith("v") else f"v{tag}"
    )
    required_assets = {
        f"pi-router-{normalized_tag}.tar.gz",
        f"pi-router-{normalized_tag}.sha256.txt",
    }
    available_assets = {
        str(item.get("name", ""))
        for item in payload.get("assets", [])
        if isinstance(item, dict)
    }

    if (
        update_available
        and not required_assets.issubset(
            available_assets
        )
    ):
        raise RuntimeError(
            "The latest release is missing its router archive or checksum."
        )

    return {
        "current_version": current_version,
        "latest_version": normalized_tag,
        "update_available": update_available,
    }


def read_shell_config_values(
    path: Path,
) -> dict[str, str]:
    values: dict[str, str] = {}

    try:
        lines = path.read_text(
            encoding="utf-8"
        ).splitlines()
    except OSError as exc:
        raise ConfigError(
            f"cannot read {path}: {exc}"
        ) from exc

    for raw in lines:
        line = raw.strip()

        if (
            not line
            or line.startswith("#")
            or "=" not in line
        ):
            continue

        key, raw_value = line.split("=", 1)
        key = key.strip()

        if not re.fullmatch(
            r"[A-Z_][A-Z0-9_]*",
            key,
        ):
            continue

        try:
            parts = shlex.split(
                raw_value,
                comments=True,
                posix=True,
            )
        except ValueError as exc:
            raise ConfigError(
                f"invalid value for {key}: {exc}"
            ) from exc

        if len(parts) != 1:
            raise ConfigError(
                f"invalid value for {key}"
            )

        values[key] = parts[0]

    return values


def read_shell_config(path: Path) -> dict[str, str]:
    values = read_shell_config_values(path)

    required = (
        "LAN_IF",
        "WAN_IF",
        "LAN_ADDR",
        "LAN_PREFIX",
    )

    missing = [
        key
        for key in required
        if not values.get(key)
    ]

    if missing:
        raise ConfigError(
            "missing configuration: "
            + ", ".join(missing)
        )

    return values


def network_from_config(
    config: dict[str, str],
) -> ipaddress.IPv4Network:
    try:
        return ipaddress.IPv4Interface(
            f"{config['LAN_ADDR']}/"
            f"{config['LAN_PREFIX']}"
        ).network
    except ValueError as exc:
        raise ConfigError(
            f"invalid LAN network: {exc}"
        ) from exc


def run_command(
    args: list[str],
    timeout: float = 8.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def parse_key_value_output(
    text: str,
) -> dict[str, str]:
    values: dict[str, str] = {}

    for line in text.splitlines():
        if ":" not in line:
            continue

        key, value = line.split(":", 1)

        values[
            key.strip().lower()
        ] = value.strip()

    return values


_status_lock = threading.Lock()
_status_cache: tuple[
    float,
    dict,
] | None = None


def proton_status() -> dict:
    global _status_cache

    now = time.monotonic()

    with _status_lock:
        if (
            _status_cache
            and now - _status_cache[0] < 4
        ):
            return _status_cache[1]

        try:
            proc = run_command(
                [
                    "protonvpn",
                    "status",
                ],
                timeout=10,
            )
        except (
            OSError,
            subprocess.TimeoutExpired,
        ) as exc:
            result = {
                "connected": False,
                "state": "unavailable",
                "detail": str(exc),
            }

            _status_cache = (
                now,
                result,
            )

            return result

        combined = "\n".join(
            part
            for part in (
                proc.stdout,
                proc.stderr,
            )
            if part
        )

        fields = parse_key_value_output(
            combined
        )

        status = fields.get(
            "status",
            "Unknown",
        )

        connected = (
            status.lower() == "connected"
        )

        result = {
            "connected": connected,
            "state": (
                status.lower().replace(
                    " ",
                    "-",
                )
                if status
                else "unknown"
            ),
            "status": status,
            "server": fields.get(
                "server",
                "",
            ),
            "load": fields.get(
                "load",
                "",
            ),
            "protocol": fields.get(
                "protocol",
                "",
            ),
        }

        if (
            proc.returncode != 0
            and status == "Unknown"
        ):
            result["state"] = "unavailable"
            result["detail"] = (
                combined.strip()[-300:]
            )

        _status_cache = (
            now,
            result,
        )

        return result


def wan_status(
    config: dict[str, str],
) -> dict:
    try:
        proc = run_command(
            [
                IP_CMD,
                "-4",
                "route",
                "show",
                "default",
                "dev",
                config["WAN_IF"],
            ],
            timeout=3,
        )
    except (
        OSError,
        subprocess.TimeoutExpired,
    ):
        return {
            "ready": False,
            "gateway": "",
        }

    line = next(
        (
            line.strip()
            for line in proc.stdout.splitlines()
            if line.strip()
        ),
        "",
    )

    gateway = ""
    fields = line.split()

    if "via" in fields:
        index = fields.index("via")

        if index + 1 < len(fields):
            gateway = fields[index + 1]

    return {
        "ready": bool(line),
        "gateway": gateway,
    }


def read_leases() -> dict[
    tuple[str, str],
    dict,
]:
    leases: dict[
        tuple[str, str],
        dict,
    ] = {}

    candidates = (
        Path(
            "/var/lib/misc/"
            "dnsmasq.leases"
        ),
        Path(
            "/var/lib/dnsmasq/"
            "dnsmasq.leases"
        ),
    )

    path = next(
        (
            candidate
            for candidate in candidates
            if candidate.is_file()
        ),
        None,
    )

    if path is None:
        return leases

    try:
        lines = path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
    except OSError:
        return leases

    for line in lines:
        fields = line.split()

        if len(fields) < 4:
            continue

        _, mac, ip, hostname = fields[:4]

        mac = mac.lower().replace(
            "-",
            ":",
        )

        if not MAC_RE.fullmatch(mac):
            continue

        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue

        leases[(mac, ip)] = {
            "mac": mac,
            "ip": ip,
            "hostname": (
                ""
                if hostname == "*"
                else hostname
            ),
            "lease": True,
        }

    return leases


def read_neighbors(
    lan_if: str,
) -> dict[
    tuple[str, str],
    dict,
]:
    neighbors: dict[
        tuple[str, str],
        dict,
    ] = {}

    try:
        proc = run_command(
            [
                IP_CMD,
                "-4",
                "neigh",
                "show",
                "dev",
                lan_if,
            ],
            timeout=3,
        )
    except (
        OSError,
        subprocess.TimeoutExpired,
    ):
        return neighbors

    for line in proc.stdout.splitlines():
        fields = line.split()

        if not fields:
            continue

        ip = fields[0]

        if "lladdr" not in fields:
            continue

        index = fields.index("lladdr")

        if index + 1 >= len(fields):
            continue

        mac = fields[
            index + 1
        ].lower().replace(
            "-",
            ":",
        )

        if not MAC_RE.fullmatch(mac):
            continue

        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue

        state = (
            fields[-1]
            if fields
            else ""
        )

        neighbors[(mac, ip)] = {
            "mac": mac,
            "ip": ip,
            "neighbor_state": state,
        }

    return neighbors


def probe_client_online(
    lan_if: str,
    mac: str,
    ip: str,
) -> bool:
    try:
        proc = run_command(
            [
                "ping",
                "-4",
                "-n",
                "-c",
                "1",
                "-W",
                "1",
                "-I",
                lan_if,
                ip,
            ],
            timeout=2,
        )
    except (
        OSError,
        subprocess.TimeoutExpired,
    ):
        return False

    if proc.returncode != 0:
        return False

    return (
        (mac, ip)
        in read_neighbors(
            lan_if
        )
    )


def read_bypass() -> dict[
    tuple[str, str],
    dict,
]:
    entries: dict[
        tuple[str, str],
        dict,
    ] = {}

    try:
        lines = BYPASS_FILE.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
    except OSError:
        return entries

    for raw in lines:
        line = raw.strip()

        if (
            not line
            or line.startswith("#")
        ):
            continue

        fields = [
            part.strip()
            for part in line.split(",")
        ]

        if len(fields) < 2:
            continue

        mac = fields[
            0
        ].lower().replace(
            "-",
            ":",
        )

        ip = fields[1]

        if not MAC_RE.fullmatch(mac):
            continue

        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue

        entries[(mac, ip)] = {
            "mac": mac,
            "ip": ip,
            "bypass": True,
        }

    return entries


def client_inventory(
    config: dict[str, str],
) -> list[dict]:
    leases = read_leases()

    neighbors = read_neighbors(
        config["LAN_IF"]
    )

    bypass = read_bypass()

    keys = (
        set(leases)
        | set(neighbors)
        | set(bypass)
    )

    online_keys = {
        key
        for key, item in neighbors.items()
        if item.get(
            "neighbor_state"
        ) == "REACHABLE"
    }

    probe_keys = sorted(
        key
        for key in keys
        if key not in online_keys
    )

    if probe_keys:
        def probe(
            key: tuple[str, str],
        ) -> bool:
            return probe_client_online(
                config["LAN_IF"],
                key[0],
                key[1],
            )

        with ThreadPoolExecutor(
            max_workers=min(
                8,
                len(probe_keys),
            )
        ) as pool:
            probe_results = pool.map(
                probe,
                probe_keys,
            )

            online_keys.update(
                key
                for key, is_online
                in zip(
                    probe_keys,
                    probe_results,
                )
                if is_online
            )

    clients: list[dict] = []

    for key in keys:
        item = {
            "mac": key[0],
            "ip": key[1],
            "hostname": "",
            "online": False,
            "neighbor_state": "",
            "bypass": key in bypass,
        }

        item.update(
            leases.get(
                key,
                {},
            )
        )

        item.update(
            neighbors.get(
                key,
                {},
            )
        )

        item["bypass"] = (
            key in bypass
        )

        item["online"] = (
            key in online_keys
        )

        clients.append(item)

    def sort_key(
        item: dict,
    ):
        try:
            return (
                0,
                int(
                    ipaddress.IPv4Address(
                        item["ip"]
                    )
                ),
                item["mac"],
            )
        except ValueError:
            return (
                1,
                0,
                item["mac"],
            )

    clients.sort(
        key=sort_key
    )

    return clients


def validate_pair(
    config: dict[str, str],
    payload: dict,
) -> tuple[str, str]:
    raw_mac = str(
        payload.get(
            "mac",
            "",
        )
    ).strip().lower().replace(
        "-",
        ":",
    )

    raw_ip = str(
        payload.get(
            "ip",
            "",
        )
    ).strip()

    if not MAC_RE.fullmatch(
        raw_mac
    ):
        raise ValueError(
            "invalid MAC address"
        )

    try:
        address = (
            ipaddress.IPv4Address(
                raw_ip
            )
        )
    except ValueError as exc:
        raise ValueError(
            "invalid IPv4 address"
        ) from exc

    network = network_from_config(
        config
    )

    router_ip = (
        ipaddress.IPv4Address(
            config["LAN_ADDR"]
        )
    )

    if (
        address not in network
        or address
        in {
            network.network_address,
            network.broadcast_address,
            router_ip,
        }
    ):
        raise ValueError(
            "IPv4 address is not "
            "a usable LAN client address"
        )

    return (
        raw_mac,
        str(address),
    )


def pair_is_observed(
    config: dict[str, str],
    mac: str,
    ip: str,
) -> bool:
    key = (
        mac,
        ip,
    )

    return (
        key in read_leases()
        or key
        in read_neighbors(
            config["LAN_IF"]
        )
    )


def ip_block_status() -> dict:
    fields: dict[str, str] = {}

    try:
        lines = (
            IP_BLOCK_STATUS_FILE.read_text(
                encoding="utf-8"
            ).splitlines()
        )

        for raw in lines:
            if "=" not in raw:
                continue

            key, value = raw.split(
                "=",
                1,
            )

            fields[key.strip()] = (
                value.strip()
            )

    except OSError:
        pass

    service_fields: dict[str, str] = {}

    try:
        proc = run_command(
            [
                SYSTEMCTL_CMD,
                "show",
                IP_REFRESH_SERVICE,
                "-p",
                "ActiveState",
                "-p",
                "StatusText",
            ],
            timeout=3,
        )

        if proc.returncode == 0:
            for raw in proc.stdout.splitlines():
                if "=" not in raw:
                    continue

                key, value = raw.split(
                    "=",
                    1,
                )

                service_fields[
                    key.strip()
                ] = value.strip()

    except (
        OSError,
        subprocess.TimeoutExpired,
    ):
        pass

    active_state = service_fields.get(
        "ActiveState",
        "",
    )

    refreshing = active_state in {
        "activating",
        "active",
        "reloading",
    }

    progress = 0
    progress_phase = ""

    status_text = service_fields.get(
        "StatusText",
        "",
    )

    match = re.fullmatch(
        r"IP_PROGRESS\|([0-9]{1,3})\|(.*)",
        status_text,
    )

    if match:
        progress = max(
            0,
            min(
                100,
                int(match.group(1)),
            ),
        )

        progress_phase = (
            match.group(2).strip()
        )

    next_refresh = ""

    try:
        proc = run_command(
            [
                SYSTEMCTL_CMD,
                "show",
                IP_REFRESH_TIMER,
                "-p",
                "NextElapseUSecRealtime",
                "--value",
            ],
            timeout=3,
        )

        if proc.returncode == 0:
            next_refresh = (
                proc.stdout.strip()
            )

    except (
        OSError,
        subprocess.TimeoutExpired,
    ):
        pass

    def read_count(
        name: str,
    ) -> int:
        try:
            return int(
                fields.get(
                    name,
                    "0",
                )
            )
        except ValueError:
            return 0

    return {
        "ready": bool(
            fields.get("updated")
        ),
        "refreshing": refreshing,
        "progress": progress,
        "progress_phase": progress_phase,
        "updated": fields.get(
            "updated",
            "",
        ),
        "ipv4_count": read_count(
            "ipv4_count"
        ),
        "cidr_count": read_count(
            "cidr_count"
        ),
        "proton_mode": fields.get(
            "proton_mode",
            "",
        ),
        "nord_mode": fields.get(
            "nord_mode",
            "",
        ),
        "hideme_mode": fields.get(
            "hideme_mode",
            "",
        ),
        "windscribe_mode": fields.get(
            "windscribe_mode",
            "",
        ),
        "privado_mode": fields.get(
            "privado_mode",
            "",
        ),
        "tunnelbear_mode": fields.get(
            "tunnelbear_mode",
            "",
        ),
        "surfshark_mode": fields.get(
            "surfshark_mode",
            "",
        ),
        "pia_mode": fields.get(
            "pia_mode",
            "",
        ),
        "tor_mode": fields.get(
            "tor_mode",
            "",
        ),
        "engine_sha": fields.get(
            "engine_sha",
            "",
        ),
        "next_refresh": next_refresh,
    }


def start_ip_block_refresh() -> tuple[
    bool,
    str,
]:
    try:
        proc = run_command(
            [
                SUDO_CMD,
                "-n",
                SYSTEMCTL_CMD,
                "--no-block",
                "start",
                IP_REFRESH_SERVICE,
            ],
            timeout=5,
        )

    except (
        OSError,
        subprocess.TimeoutExpired,
    ) as exc:
        return (
            False,
            str(exc),
        )

    output = "\n".join(
        part.strip()
        for part in (
            proc.stdout,
            proc.stderr,
        )
        if part.strip()
    )

    if proc.returncode != 0:
        return (
            False,
            (
                output[-500:]
                if output
                else (
                    "Unable to start "
                    "IP blocklist refresh."
                )
            ),
        )

    return (
        True,
        "IP blocklist refresh started.",
    )


def start_router_update() -> tuple[
    bool,
    str,
]:
    try:
        proc = run_command(
            [
                SUDO_CMD,
                "-n",
                SYSTEMCTL_CMD,
                "--no-block",
                "start",
                UPDATE_SERVICE,
            ],
            timeout=5,
        )
    except (
        OSError,
        subprocess.TimeoutExpired,
    ) as exc:
        return False, str(exc)

    output = "\n".join(
        part.strip()
        for part in (
            proc.stdout,
            proc.stderr,
        )
        if part.strip()
    )

    if proc.returncode != 0:
        return (
            False,
            output[-500:]
            if output
            else "Unable to start the router update.",
        )

    return (
        True,
        "Update started. The router will reboot automatically when installation completes.",
    )


def set_bypass(
    config: dict[str, str],
    action: str,
    mac: str,
    ip: str,
) -> tuple[
    bool,
    str,
]:
    existing = read_bypass()

    key = (
        mac,
        ip,
    )

    if (
        action == "add"
        and key in existing
    ):
        return (
            True,
            "Client is already "
            "bypassing Proton.",
        )

    if (
        action == "remove"
        and key not in existing
    ):
        return (
            True,
            "Client is already "
            "using Proton.",
        )

    if (
        action == "add"
        and not pair_is_observed(
            config,
            mac,
            ip,
        )
    ):
        return (
            False,
            "Client MAC/IP pair is "
            "not currently known "
            "to the router.",
        )

    try:
        proc = run_command(
            [
                SUDO_CMD,
                "-n",
                PROTON_BYPASS,
                action,
                mac,
                ip,
            ],
            timeout=12,
        )
    except (
        OSError,
        subprocess.TimeoutExpired,
    ) as exc:
        return (
            False,
            str(exc),
        )

    output = "\n".join(
        part.strip()
        for part in (
            proc.stdout,
            proc.stderr,
        )
        if part.strip()
    )

    return (
        proc.returncode == 0,
        (
            output[-500:]
            if output
            else "Done."
        ),
    )


class RouterWebServer(
    ThreadingHTTPServer
):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address,
        handler_class,
        static_root: Path,
    ):
        super().__init__(
            server_address,
            handler_class,
        )

        self.static_root = (
            static_root.resolve()
        )

        try:
            self.router_version = (
                self.static_root
                .joinpath("version.txt")
                .read_text(encoding="utf-8")
                .strip()
            )
        except OSError:
            self.router_version = DEFAULT_VERSION


class Handler(
    BaseHTTPRequestHandler
):
    server_version = (
        "PiRouterWeb/1.0"
    )

    def log_message(
        self,
        fmt: str,
        *args,
    ) -> None:
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (
                self.client_address[0],
                self.log_date_time_string(),
                fmt % args,
            )
        )

    def current_config(
        self,
    ) -> dict[str, str]:
        return read_shell_config(
            CONFIG_FILE
        )

    def client_allowed(
        self,
        config: dict[str, str],
    ) -> bool:
        try:
            remote = (
                ipaddress.ip_address(
                    self.client_address[0]
                )
            )
        except ValueError:
            return False

        if remote.is_loopback:
            return True

        return (
            isinstance(
                remote,
                ipaddress.IPv4Address,
            )
            and remote
            in network_from_config(
                config
            )
        )

    def host_allowed(
        self,
        config: dict[str, str],
    ) -> bool:
        host = self.headers.get(
            "Host",
            "",
        )

        if not host:
            return False

        hostname = host.rsplit(
            ":",
            1,
        )[0].strip("[]").lower()

        return hostname in {
            config["LAN_ADDR"].lower(),
            "127.0.0.1",
            "localhost",
        }

    def same_origin(
        self,
    ) -> bool:
        origin = self.headers.get(
            "Origin"
        )

        if not origin:
            return True

        try:
            parsed = urlsplit(
                origin
            )
        except ValueError:
            return False

        return (
            parsed.scheme
            in {
                "http",
                "https",
            }
            and parsed.netloc
            == self.headers.get(
                "Host",
                "",
            )
        )

    def security_headers(
        self,
    ) -> None:
        self.send_header(
            "X-Content-Type-Options",
            "nosniff",
        )

        self.send_header(
            "X-Frame-Options",
            "DENY",
        )

        self.send_header(
            "Referrer-Policy",
            "no-referrer",
        )

        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "connect-src 'self'; "
            "img-src 'self'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'",
        )

    def send_json(
        self,
        payload: dict | list,
        status: HTTPStatus = (
            HTTPStatus.OK
        ),
    ) -> None:
        body = json.dumps(
            payload,
            separators=(
                ",",
                ":",
            ),
        ).encode(
            "utf-8"
        )

        self.send_response(
            status
        )

        self.send_header(
            "Content-Type",
            "application/json; "
            "charset=utf-8",
        )

        self.send_header(
            "Content-Length",
            str(len(body)),
        )

        self.send_header(
            "Cache-Control",
            "no-store",
        )

        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def fail_json(
        self,
        status: HTTPStatus,
        message: str,
    ) -> None:
        self.send_json(
            {
                "ok": False,
                "error": message,
            },
            status,
        )

    def guard(
        self,
        mutation: bool = False,
    ) -> dict[str, str] | None:
        try:
            config = (
                self.current_config()
            )
        except ConfigError as exc:
            self.fail_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                str(exc),
            )

            return None

        if not self.client_allowed(
            config
        ):
            self.fail_json(
                HTTPStatus.FORBIDDEN,
                "LAN access only",
            )

            return None

        if not self.host_allowed(
            config
        ):
            self.fail_json(
                HTTPStatus.FORBIDDEN,
                "invalid Host header",
            )

            return None

        if (
            mutation
            and not self.same_origin()
        ):
            self.fail_json(
                HTTPStatus.FORBIDDEN,
                "cross-origin request denied",
            )

            return None

        return config

    def do_GET(
        self,
    ) -> None:
        config = self.guard()

        if config is None:
            return

        path = urlsplit(
            self.path
        ).path

        if path == "/api/status":
            self.send_json(
                {
                    "ok": True,
                    "version": (
                        self.server.router_version
                    ),
                    "proton": (
                        proton_status()
                    ),
                    "wan": (
                        wan_status(
                            config
                        )
                    ),
                    "lan": {
                        "interface": (
                            config[
                                "LAN_IF"
                            ]
                        ),
                        "address": (
                            config[
                                "LAN_ADDR"
                            ]
                        ),
                    },
                }
            )

            return

        if path == "/api/clients":
            clients = (
                client_inventory(
                    config
                )
            )

            self.send_json(
                {
                    "ok": True,
                    "clients": clients,
                    "counts": {
                        "total": len(
                            clients
                        ),
                        "online": sum(
                            1
                            for item
                            in clients
                            if item.get(
                                "online"
                            )
                        ),
                        "bypass": sum(
                            1
                            for item
                            in clients
                            if item.get(
                                "bypass"
                            )
                        ),
                    },
                }
            )

            return

        if path == "/api/ip-block":
            self.send_json(
                {
                    "ok": True,
                    "ip_block": (
                        ip_block_status()
                    ),
                }
            )

            return

        if path == "/api/update/check":
            try:
                release = latest_release(
                    self.server.router_version
                )
            except (ConfigError, RuntimeError) as exc:
                self.fail_json(
                    HTTPStatus.BAD_GATEWAY,
                    str(exc),
                )

                return

            self.send_json(
                {
                    "ok": True,
                    **release,
                }
            )

            return

        if path == "/api/bypass":
            self.send_json(
                {
                    "ok": True,
                    "bypass": list(
                        read_bypass().values()
                    ),
                }
            )

            return

        self.serve_static(
            path
        )

    def do_POST(
        self,
    ) -> None:
        config = self.guard(
            mutation=True
        )

        if config is None:
            return

        path = urlsplit(
            self.path
        ).path

        if path == "/api/ip-block/refresh":
            self.handle_ip_block_refresh()
            return

        if path == "/api/update":
            self.handle_router_update()
            return

        if path != "/api/bypass":
            self.fail_json(
                HTTPStatus.NOT_FOUND,
                "not found",
            )

            return

        self.handle_bypass(
            config,
            "add",
        )

    def do_DELETE(
        self,
    ) -> None:
        config = self.guard(
            mutation=True
        )

        if config is None:
            return

        if (
            urlsplit(
                self.path
            ).path
            != "/api/bypass"
        ):
            self.fail_json(
                HTTPStatus.NOT_FOUND,
                "not found",
            )

            return

        self.handle_bypass(
            config,
            "remove",
        )

    def handle_ip_block_refresh(
        self,
    ) -> None:
        ok, message = (
            start_ip_block_refresh()
        )

        if not ok:
            self.fail_json(
                HTTPStatus.CONFLICT,
                (
                    message
                    or (
                        "Unable to start "
                        "IP blocklist refresh."
                    )
                ),
            )

            return

        self.send_json(
            {
                "ok": True,
                "message": message,
                "ip_block": (
                    ip_block_status()
                ),
            }
        )

    def handle_router_update(
        self,
    ) -> None:
        try:
            update_repository()
        except ConfigError as exc:
            self.fail_json(
                HTTPStatus.CONFLICT,
                str(exc),
            )

            return

        ok, message = start_router_update()

        if not ok:
            self.fail_json(
                HTTPStatus.CONFLICT,
                message or "Unable to start the router update.",
            )

            return

        self.send_json(
            {
                "ok": True,
                "message": message,
            },
            HTTPStatus.ACCEPTED,
        )


    def handle_bypass(
        self,
        config: dict[str, str],
        action: str,
    ) -> None:
        if (
            self.headers.get_content_type()
            != "application/json"
        ):
            self.fail_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "application/json required",
            )

            return

        try:
            length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
            )
        except ValueError:
            self.fail_json(
                HTTPStatus.BAD_REQUEST,
                "invalid Content-Length",
            )

            return

        if (
            length <= 0
            or length > MAX_BODY
        ):
            self.fail_json(
                HTTPStatus.BAD_REQUEST,
                "invalid request size",
            )

            return

        try:
            payload = json.loads(
                self.rfile.read(
                    length
                ).decode(
                    "utf-8"
                )
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            self.fail_json(
                HTTPStatus.BAD_REQUEST,
                "invalid JSON",
            )

            return

        if not isinstance(
            payload,
            dict,
        ):
            self.fail_json(
                HTTPStatus.BAD_REQUEST,
                "JSON object required",
            )

            return

        try:
            mac, ip = validate_pair(
                config,
                payload,
            )
        except ValueError as exc:
            self.fail_json(
                HTTPStatus.BAD_REQUEST,
                str(exc),
            )

            return

        ok, message = set_bypass(
            config,
            action,
            mac,
            ip,
        )

        if not ok:
            self.fail_json(
                HTTPStatus.CONFLICT,
                message
                or "operation failed",
            )

            return

        self.send_json(
            {
                "ok": True,
                "message": message,
                "mac": mac,
                "ip": ip,
            }
        )

    def serve_static(
        self,
        request_path: str,
    ) -> None:
        routes = {
            "/": "index.html",
            "/index.html": "index.html",
            "/app.js": "app.js",
            "/styles.css": "styles.css",
            "/favicon.svg": "favicon.svg",
            "/favicon.ico": "favicon.ico"
        }

        filename = routes.get(
            request_path
        )

        if not filename:
            self.send_error(
                HTTPStatus.NOT_FOUND
            )

            return

        target = (
            self.server.static_root
            / filename
        )

        try:
            body = target.read_bytes()
        except OSError:
            self.send_error(
                HTTPStatus.NOT_FOUND
            )

            return

        content_type = (
            mimetypes.guess_type(
                filename
            )[0]
            or "application/octet-stream"
        )

        self.send_response(
            HTTPStatus.OK
        )

        self.send_header(
            "Content-Type",
            f"{content_type}; "
            "charset=utf-8",
        )

        self.send_header(
            "Content-Length",
            str(len(body)),
        )

        self.send_header(
            "Cache-Control",
            "no-cache",
        )

        self.security_headers()
        self.end_headers()
        self.wfile.write(body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Raspberry Pi Proton "
            "router web UI"
        )
    )

    parser.add_argument(
        "--bind",
        default="0.0.0.0",
        help="IPv4 address to bind",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="TCP port",
    )

    parser.add_argument(
        "--static-root",
        type=Path,
        default=DEFAULT_STATIC_ROOT,
        help=(
            "directory containing "
            "index.html, app.js and "
            "styles.css"
        ),
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        config = read_shell_config(
            CONFIG_FILE
        )

        network_from_config(
            config
        )
    except ConfigError as exc:
        print(
            f"pi-router-web: {exc}",
            file=sys.stderr,
        )

        return 1

    if not (
        1 <= args.port <= 65535
    ):
        print(
            "pi-router-web: "
            "invalid port",
            file=sys.stderr,
        )

        return 1

    if not args.static_root.is_dir():
        print(
            "pi-router-web: "
            "static directory not found: "
            f"{args.static_root}",
            file=sys.stderr,
        )

        return 1

    server = RouterWebServer(
        (
            args.bind,
            args.port,
        ),
        Handler,
        args.static_root,
    )

    print(
        "pi-router-web: serving "
        f"http://{args.bind}:"
        f"{args.port}/ "
        "for LAN "
        f"{network_from_config(config)}",
        file=sys.stderr,
    )

    try:
        server.serve_forever(
            poll_interval=0.5
        )
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
