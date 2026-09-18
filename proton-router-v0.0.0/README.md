# Raspberry Pi 5 Proton Router

A Raspberry Pi 5 configured as a fail-closed home router with:

- Ethernet WAN and LAN
- Proton VPN for normal client traffic
- Per-device raw-WAN bypass
- Cloudflare Families DNS enforcement
- DNS redirection and common encrypted-DNS blocking
- Dynamic ISP DHCP support
- Automatic Proton reconnection
- Automatic empty-password GNOME login keyring initialization
- Centralized router settings in `/etc/proton-router.conf`

## Network topology

```text
ISP modem
    |
    | WAN
    v
 Raspberry Pi 5
 eth1 = WAN (DHCP)
 eth0 = LAN (static)
    |
    v
Wireless AP / switch
    |
    +-- normal clients -> Proton VPN
    |
    +-- selected bypass devices -> raw ISP WANDefault example LAN:

Router:       192.168.50.1/24
DHCP pool:    192.168.50.100 - 192.168.50.220
LAN device:   eth0
WAN device:   eth1

These values are configurable in config/proton-router.conf.example.

Core behavior

Normal LAN clients are forwarded through Proton VPN.

Devices explicitly added with proton-bypass are policy-routed directly through the ISP WAN while still using the router's DNS and firewall enforcement.

The firewall is fail-closed:

normal LAN clients cannot fall back to the raw WAN if Proton fails
bypass clients use a dedicated policy-routing table
if the WAN route is unavailable, the bypass table contains a blackhole default
Pi-originated raw-WAN traffic is blocked except for explicitly permitted Proton bootstrap/transport traffic
DNS policy

dnsmasq provides LAN DHCP and DNS.

The default upstream is Cloudflare Families:

1.1.1.3

LAN DNS traffic on TCP/UDP port 53 is redirected to the router.

The firewall also blocks:

DNS-over-TLS on TCP/UDP 853
UDP 443 to disable QUIC/HTTP3 paths
TCP 443 to selected well-known public DNS resolver addresses

dnsmasq caching is intentionally disabled with:

cache-size=0

Do not remove this setting. It prevents cached CNAME target addresses from bypassing the intended Cloudflare Families SafeSearch behavior.

Repository layout
.
├── README.md
├── requirements-apt.txt
├── requirements-optional.txt
├── config/
│   ├── 99-router.conf
│   ├── NetworkManager-wait-online.override.conf
│   ├── dnsmasq-router.conf
│   ├── gnome-keyring-secrets.desktop
│   ├── nftables.conf
│   ├── proton-bypass-devices.conf.example
│   ├── proton-router.conf.example
│   └── protonvpn-autoconnect.service
├── scripts/
│   ├── proton-bypass
│   ├── proton-keyring-init
│   ├── proton-router-apply-conf
│   └── protonvpn-autoconnect
└── docs/
Configuration model

The primary configuration file is:

/etc/proton-router.conf

It controls:

router user
desktop session
LAN interface
WAN interface
LAN address and prefix
DHCP range and lease time
upstream DNS server
bypass policy-routing mark
bypass routing-table number
bypass policy-rule priority

Run:

sudo proton-router-apply-conf

after changing the configuration.

proton-router-apply-conf generates machine-specific configuration for:

nftables variables
dnsmasq variables
LightDM autologin
NetworkManager polkit permissions
Proton bootstrap sudoers permissions
proton-keyring-init systemd service

Generated files are intentionally not stored in this repository.

Bypass devices

Add a device:

sudo proton-bypass add aa:bb:cc:dd:ee:ff 192.168.50.180

List configured devices:

sudo proton-bypass list

Remove a device:

sudo proton-bypass remove aa:bb:cc:dd:ee:ff 192.168.50.180

Real bypass-device MAC/IP assignments belong in:

/etc/proton-bypass/devices.conf

They are intentionally excluded from Git.

Proton VPN

This build uses the official Proton VPN Linux CLI.

Tested configuration:

kill-switch: standard
ipv6:       off
protocol:   WireGuard

The Proton account must be signed in once on the router.

Credentials, Proton session data, GNOME keyrings, and WireGuard private keys must never be committed to Git.

Automatic connection is handled by the user service:

protonvpn-autoconnect.service

The autoconnect script waits for the configured WAN interface to receive a usable IPv4 default route before attempting Proton connection.

NetworkManager startup

The WAN uses DHCP and may legitimately need multiple attempts when an ISP modem is slow to provide a lease.

The included NetworkManager-wait-online override allows local router startup to complete once NetworkManager has connectivity rather than requiring every WAN DHCP autoconnect attempt to settle.

WAN DHCP retries continue independently.

Secrets and generated state

Never commit:

/etc/wireguard/proton0.conf
~/.local/share/keyrings/*
/etc/proton-bypass/devices.conf

Also do not commit generated files such as:

/etc/nftables.d/generated-vars.nft
/etc/dnsmasq.d/generated-vars.conf
/etc/lightdm/lightdm.conf.d/90-proton-router-autologin.conf
/etc/polkit-1/rules.d/49-protonvpn-networkmanager.rules
/etc/sudoers.d/protonvpn-bootstrap
/etc/systemd/system/proton-keyring-init.service
Platform

Developed and tested on Raspberry Pi 5 running Raspberry Pi OS Desktop / Debian 13 (trixie) arm64.

See the files in docs/ for installation, Proton setup, architecture, and verification details.
