# Raspberry Pi 5 Proton Router

A Raspberry Pi 5 configured as a fail-closed home router for Proton VPN, with per-device raw-WAN bypass, enforced DNS policy, dynamic VPN/proxy/Tor endpoint blocking, and a local web management interface.

## Features

- 1 GBps Ethernet WAN and LAN routing
- Proton VPN for normal LAN client traffic
- Device raw-WAN Proton VPN bypass with fail-closed policy routing
- Cloudflare Families DNS (`1.1.1.3`) by default
- Automatic Proton VPN reconnection after boot
- Bounded Proton VPN connection retries for transient session failures
- Automatic empty-password GNOME login keyring initialization for unattended Proton VPN startup
- Automatic endpoint refresh every six hours
- Local web UI for router/VPN status, endpoint-block status, and client bypass control
- Manual endpoint block list refresh from local web UI
- Dynamic blocking of known commercial VPN, open-proxy, and Tor relay endpoints
- Static DNS blocking for selected proxy/remote-browser domains
- Windows installer and manual Linux install steps available

## Tested platform

Developed and tested on:

- Raspberry Pi 5
- Raspberry Pi OS Desktop, 64-bit
- Debian 13 / trixie
- NetworkManager
- LightDM
- `rpd-labwc`
- Proton VPN Linux CLI

The tested network layout uses:

- built-in Pi RJ45 as `eth0` / LAN
- second Ethernet adapter as `eth1` / WAN

> Proton does not officially support Raspberry Pi OS/Raspbian. This project uses Proton's Debian ARM64 packages and has been tested successfully on Raspberry Pi OS Desktop, but this is not an officially supported Proton platform.

## Network topology

```text
ISP modem / upstream router
        |
        | WAN DHCP
        v
 Raspberry Pi 5
 eth1 = WAN
 eth0 = LAN
        |
        v
 access point / switch
        |
        +-- normal clients ------> Proton VPN
        |
        +-- selected clients ----> raw ISP WAN bypass
```

The downstream wireless device is used in access-point/bridge mode rather than router/NAT mode. Connect the Pi's `eth0` LAN interface to a LAN port on the access point. The access point's WAN/routing function is not used.

The Raspberry Pi is the router. `eth1` is its upstream/WAN interface and `eth0` is its downstream/LAN interface. Keeping the access point bridged allows the Pi to see and apply policy to individual LAN clients.

Default example LAN:

```text
Router:       192.168.50.1/24
DHCP pool:    192.168.50.100 - 192.168.50.220
LAN device:   eth0
WAN device:   eth1
```

These values are configurable in `config/proton-router.conf.example` and are installed as `/etc/proton-router.conf`.

## Core routing behavior

Normal LAN clients are forwarded through Proton VPN.

Devices explicitly added with `proton-bypass` are policy-routed directly through the ISP WAN while still using the router's DNS and firewall policy.

The router is designed to fail closed:

- normal LAN clients cannot fall back to raw WAN if Proton is unavailable
- bypass clients use a dedicated policy-routing table
- the bypass table contains a blackhole default when usable WAN routing is unavailable
- Raw WAN traffic from the Pi is blocked except for WAN DHCP and the dedicated Proton VPN transport
- the temporary Proton bootstrap gate is closed again on success and failure

The default bypass routing internals are:

```text
mark:      0x50564250
table:     200
rule prio: 10000
```

## DNS policy

`dnsmasq` provides DHCP and DNS to LAN clients.

The default upstream is Cloudflare Families:

```text
1.1.1.3
```

LAN DNS traffic on TCP/UDP port 53 is redirected to the router.

The firewall also blocks common DNS-bypass paths including:

- DNS-over-TLS on TCP/UDP 853
- UDP 443 to prevent common QUIC/HTTP3 bypass paths
- TCP 443 to selected well-known public DNS resolver addresses

Static named-domain policy is installed from:

```text
/etc/dnsmasq.d/90-proton-router-domain-block.conf
```

This includes selected proxy/remote-browser domains and related policy entries. The authoritative Tor relay feed dependency at `onionoo.torproject.org` is explicitly exempted from the parent Tor Project DNS block so the endpoint refresh engine can continue to retrieve relay data.

`dnsmasq` caching is intentionally disabled:

```text
cache-size=0
```

Do not remove this setting without reviewing the DNS enforcement model.

## Dynamic VPN, proxy, and Tor endpoint blocking

The router maintains an nftables set of known VPN, open-proxy, and Tor IPv4 endpoints used to prevent LAN clients from directly reaching alternate routing infrastructure and bypassing router policy.

The refresh pipeline currently consumes provider data for:

- Proton VPN
- NordVPN
- hide.me
- Windscribe
- PrivadoVPN
- TunnelBear
- Surfshark
- Private Internet Access (PIA)
- [IPLocate Free Proxy List](https://github.com/iplocate/free-proxy-list)
- Tor running relays via Onionoo

Provider-specific adapters normalize the different upstream formats into one endpoint database. The combined database is validated before it is published and loaded into nftables.

IPLocate publishes open-proxy endpoints as `protocol://IPv4:port`. Its adapter validates all three fields, preserves the protocol and port as provenance, and submits the bare IPv4 address to the existing whole-address blocking pipeline.

If a current provider feed or adapter fails, the refresh can use that provider's validated last-known-good data. If a required provider has neither usable current data nor a last-known-good copy, the refresh fails instead of publishing a partial database. An existing successfully published database remains available.

The published nftables set is:

```text
blocked_vpn_endpoints_v4
```

Automatic refresh is handled by:

```text
proton-router-ip-refresh.timer
```

The default schedule is every six hours:

```text
00:00
06:00
12:00
18:00
```

The initial endpoint-database build is not required to complete installation. The refresh timer and manual refresh capability are installed during setup, and the database can be populated afterward by the timer or from the web UI.

Useful diagnostics:

```bash
sudo systemctl status proton-router-ip-refresh.service
sudo systemctl status proton-router-ip-refresh.timer
sudo journalctl -u proton-router-ip-refresh.service --no-pager
```

A manual CLI refresh can be started with:

```bash
sudo systemctl start proton-router-ip-refresh.service
```

## Local web interface

The router includes a small web UI implemented with Python's standard library and static HTML/CSS/JavaScript. No nginx, Apache, Node.js, external web framework, or database is required.

Default URL:

```text
http://192.168.50.1:8080/
```

The interface shows:

- Proton VPN connection state
- Proton server, protocol, and load when available
- WAN readiness and current gateway
- LAN/router information
- known LAN clients
- current per-client Proton/raw-WAN bypass state
- IP blocklist database status
- endpoint count and normalized CIDR count
- last and next endpoint refresh information
- live progress for a manual endpoint refresh

The UI can:

- add/remove bypass for known LAN MAC/IPv4 pairs
- trigger the IP blocklist refresh service
- refresh displayed client/status information

The backend exposes purpose-specific API routes rather than a general shell endpoint.

The web server listens on IPv4 port 8080, while the router's nftables input policy permits access from the LAN and loopback and drops unsolicited WAN input. The web interface should never be exposed by adding a WAN allow rule or port forward.

## Proton VPN behavior

This build uses the official Proton VPN Linux CLI.

Tested configuration:

```text
kill-switch: standard
ipv6:       off
protocol:   WireGuard
```

The Proton account is signed in once during the post-reboot setup phase. Credentials and session state remain local to the router account/keyring.

Automatic connection is handled by:

```text
~/.local/bin/protonvpn-autoconnect
protonvpn-autoconnect.service
```

The helper:

1. waits for a usable IPv4 default route on the configured WAN interface
2. waits for the user's Secret Service/keyring
3. exits successfully if Proton is already connected
4. temporarily opens the dedicated host bootstrap firewall gate
5. runs the Proton connection attempt with bounded retries
6. closes the bootstrap gate on every exit path

Do not use bare `protonvpn connect` for normal router operation. The managed helper is the supported connection path because it coordinates Proton with the fail-closed host firewall.

You can also run `systemctl --user --no-block restart protonvpn-autoconnect.service` like the health check

## Installation model

`setup` is the canonical implementation of the router installation. The Windows installer and the manual Linux procedure drive the same Pi-side setup phases rather than maintaining separate installation logic.

Canonical phase flow:

```text
pre-reboot
    |
    +-- optional Wi-Fi -> LAN management handoff
    |
pre-reboot-resume
    |
    v
first reboot
    |
    v
post-reboot
    |
    v
LAN DHCP promotion / client lease refresh
    |
    v
final unattended reboot
    |
    v
wait for unattended Proton
    |
    v
final-verify
```

For the full manual Linux procedure, see:

```text
docs/setup.md
```

A successful installation must ultimately reach:

```text
FINAL VERIFICATION PASSED
```

## Configuration model

The primary installed configuration file is:

```text
/etc/proton-router.conf
```

It controls:

- router user
- desktop session
- LAN interface
- WAN interface
- LAN address and prefix
- DHCP range and lease time
- upstream DNS server
- bypass policy-routing mark
- bypass routing-table number
- bypass policy-rule priority

After changing the installed configuration, run:

```bash
sudo proton-router-apply-conf
```

`proton-router-apply-conf` generates and applies machine-specific configuration including:

- nftables variables
- dnsmasq variables
- LightDM autologin configuration
- NetworkManager polkit policy
- restricted sudoers policy for Proton VPN, web bypass add/remove operations, and exact endpoint refresh service
- keyring initialization service
- bypass routing state

Generated machine-specific files are intentionally not stored in Git.

## Per-device raw-WAN bypass

Add a device:

```bash
sudo proton-bypass add aa:bb:cc:dd:ee:ff 192.168.50.180
```

List configured devices:

```bash
sudo proton-bypass list
```

Remove a device:

```bash
sudo proton-bypass remove aa:bb:cc:dd:ee:ff 192.168.50.180
```

Real bypass-device MAC/IP assignments are stored in:

```text
/etc/proton-bypass/devices.conf
```

They must not be committed to Git.

The same add/remove operations can be requested through the local web UI for LAN clients already known to the router.

## NetworkManager startup

The WAN uses DHCP and may legitimately require multiple attempts when an upstream modem/router is slow to provide a lease.

The included `NetworkManager-wait-online` override allows local router startup to continue once NetworkManager has usable connectivity rather than requiring every autoconnect attempt to settle.

WAN DHCP retries continue independently.

## Src folder layout

```text
src/
├── README.md
├── setup
├── requirements-apt.txt
├── requirements-optional.txt
├── config/
│   ├── 90-proton-router-domain-block.conf
│   ├── 99-router.conf
│   ├── NetworkManager-wait-online.override.conf
│   ├── dnsmasq-router.conf
│   ├── gnome-keyring-secrets.desktop
│   ├── nftables.conf
│   ├── proton-bypass-devices.conf.example
│   ├── proton-repository.conf
│   ├── proton-router-ip-refresh.service
│   ├── proton-router-ip-refresh.timer
│   ├── proton-router-web.service
│   ├── proton-router.conf.example
│   ├── protonvpn-autoconnect.service
│   ├── protonvpn-healthcheck.service
│   └── protonvpn-healthcheck.timer
├── docs/
│   └── setup.md
├── lib/
│   └── ip-block/
│       ├── normalize.py
│       └── adapters/
│           ├── hideme_adapter.py
│           ├── iplocate_adapter.py
│           ├── nord_adapter.py
│           ├── pia_adapter.py
│           ├── privado_adapter.py
│           ├── proton_adapter.py
│           ├── surfshark_adapter.py
│           ├── tor_adapter.py
│           ├── tunnelbear_adapter.py
│           └── windscribe_adapter.py
├── scripts/
│   ├── proton-bypass
│   ├── proton-keyring-init
│   ├── proton-router-apply-conf
│   ├── proton-router-ip-refresh
│   ├── protonvpn-autoconnect
│   └── protonvpn-healthcheck
└── web/
    ├── app.js
    ├── favicon.ico
    ├── favicon.svg
    ├── index.html
    ├── server.py
    └── styles.css
```

## Secrets and generated state

Never commit credentials, keyrings, VPN private keys, or real client identities.

In particular, do not commit or copy from an existing configured router:

```text
/etc/wireguard/proton0.conf
~/.local/share/keyrings/*
/etc/proton-bypass/devices.conf
```

Also do not commit machine-generated files such as:

```text
/etc/nftables.d/generated-vars.nft
/etc/dnsmasq.d/generated-vars.conf
/etc/lightdm/lightdm.conf.d/90-proton-router-autologin.conf
/etc/polkit-1/rules.d/49-protonvpn-networkmanager.rules
/etc/sudoers.d/protonvpn-bootstrap
/etc/systemd/system/proton-keyring-init.service
```

IP blocklist run snapshots and last-known-good provider data under `/var/lib/proton-router/ip-block/` are runtime state and should not be treated as repository source.

## Quick verification

Useful checks on a completed router:

```bash
protonvpn status
nmcli radio wifi
sysctl net.ipv4.ip_forward
ip -4 rule show
ip -4 route show table 200
systemctl --user status protonvpn-autoconnect.service --no-pager
systemctl --user status proton-router-web.service --no-pager
sudo systemctl status proton-router-ip-refresh.timer --no-pager
```

The canonical end-to-end verification is:

```bash
cd ~/proton-router-vX.Y.Z # Replace with the release version, for example 0.1.0

./setup \
    --live \
    --allow-live-install \
    --phase final-verify \
    --non-interactive
```

Expected final result:

```text
FINAL VERIFICATION PASSED
```
