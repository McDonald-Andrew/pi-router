# Fresh Raspberry Pi Setup

This document builds the router from a fresh Raspberry Pi OS installation using only the files in this repository plus the official Proton VPN package repository.

The goal is not merely to describe the existing router. This procedure is intended to be a reproducibility test: a fresh Raspberry Pi should be able to follow these steps and arrive at the same fail-closed router configuration, including the local web management interface.

## 1. Starting point

Tested hardware:

- Raspberry Pi 5
- built-in Ethernet used as LAN
- second Ethernet adapter used as WAN
- external wireless access point connected to the LAN

Tested software baseline:

- Raspberry Pi OS Desktop, 64-bit
- Debian 13 / trixie
- NetworkManager
- LightDM
- `rpd-labwc` desktop session
- Proton VPN Linux CLI

Raspberry Pi OS Desktop is recommended rather than Raspberry Pi OS Lite because this design uses:

- LightDM autologin
- a desktop user session
- GNOME Secret Service / GNOME Keyring
- the Proton VPN Linux CLI
- user-level systemd services for Proton autoconnect and the router web interface

> Proton does not officially support Raspberry Pi OS/Raspbian. This project uses the Debian ARM64 packages and has been tested successfully on Raspberry Pi OS Desktop, but this is not an officially supported Proton platform.

Do not put Proton credentials, keyrings, WireGuard private keys, or real bypass-device identities into this repository.

---

## 2. Install Raspberry Pi OS

Flash a fresh 64-bit Raspberry Pi OS Desktop image.

During initial setup, create the account that will own the Proton VPN session.

The repository example uses:

```text
admin
```

A different username is supported. It must match `ROUTER_USER` in `/etc/proton-router.conf`.

Before changing networking, update the OS:

```bash
sudo apt update
sudo apt full-upgrade -y
```

A reboot after the OS upgrade is recommended if the upgrade installed a new kernel:

```bash
sudo reboot
```

---

## 3. Place the router release on the Pi

For exact parity with the automated installer, place the router release tree at:

```text
~/proton-router-v0.0.0
```

All remaining relative paths in this guide assume the shell is in the repository root.

---

## 4. Identify the network interfaces

Check the available interfaces:

```bash
ip -br link
```

The tested hardware uses:

```text
eth0 = LAN
eth1 = WAN
```

Do not assume those names on different hardware.

The WAN interface should be the adapter connected to the ISP modem.

The LAN interface should be the adapter connected to the downstream switch or wireless access point.

Also verify the desktop session:

```bash
ls /usr/share/wayland-sessions
ls /usr/share/xsessions 2>/dev/null || true
```

The tested Raspberry Pi OS session is:

```text
rpd-labwc
```

---

## 5. Install Proton VPN and router packages

Add Proton's official stable Debian package repository using the current instructions from Proton's Debian/Ubuntu installation documentation.

Repository bootstrap packages and checksums can change over time, so this project does not duplicate that bootstrap package.

After the Proton repository has been added:

```bash
sudo apt update
```

Install the runtime packages listed by this project:

```bash
grep -vE '^[[:space:]]*(#|$)' requirements-apt.txt |
    xargs -r sudo apt install -y
```

The required project packages are currently:

```text
dnsmasq
nftables
proton-vpn-cli
```

Raspberry Pi OS Desktop supplies the other platform components used by the scripts, including NetworkManager, Python 3, systemd, sudo, D-Bus, and the desktop environment.

Verify the important commands exist:

```bash
for cmd in \
    protonvpn \
    dnsmasq \
    nft \
    nmcli \
    ip \
    python3 \
    gnome-keyring-daemon \
    dbus-run-session \
    gdbus \
    systemd-analyze \
    visudo
do
    command -v "$cmd" || echo "MISSING: $cmd"
done
```

Do not continue if any required command is reported missing.

---

## 6. Install the master router configuration

Install the example configuration:

```bash
sudo install -m 0644 \
    config/proton-router.conf.example \
    /etc/proton-router.conf
```

Edit it:

```bash
sudo nano /etc/proton-router.conf
```

At minimum, verify:

```text
ROUTER_USER
ROUTER_SESSION
LAN_IF
WAN_IF
LAN_ADDR
LAN_PREFIX
DHCP_START
DHCP_END
DHCP_LEASE
DNS_UPSTREAM
```

The tested defaults are:

```text
ROUTER_USER="admin"
ROUTER_SESSION="rpd-labwc"

LAN_IF="eth0"
WAN_IF="eth1"

LAN_ADDR="192.168.50.1"
LAN_PREFIX="24"

DHCP_START="192.168.50.100"
DHCP_END="192.168.50.220"
DHCP_LEASE="12h"

DNS_UPSTREAM="1.1.1.3"
```

The bypass mark, routing table, and policy-rule priority normally do not need changing:

```text
BYPASS_MARK="0x50564250"
BYPASS_TABLE="200"
BYPASS_RULE_PRIORITY="10000"
```

Load the configuration into the current shell:

```bash
. /etc/proton-router.conf
```

For this setup procedure, the current desktop account should be the configured router account:

```bash
test "$(id -un)" = "$ROUTER_USER" &&
    echo "router user OK"
```

If that does not print `router user OK`, stop and log in as the configured router user.

---

## 7. Install the core system files

Install the router scripts:

```bash
sudo install -m 0755 \
    scripts/proton-router-apply-conf \
    /usr/local/sbin/proton-router-apply-conf

sudo install -m 0755 \
    scripts/proton-bypass \
    /usr/local/sbin/proton-bypass

sudo install -m 0755 \
    scripts/proton-keyring-init \
    /usr/local/sbin/proton-keyring-init
```

Install the static nftables configuration:

```bash
sudo install -m 0644 \
    config/nftables.conf \
    /etc/nftables.conf
```

Install the static dnsmasq policy:

```bash
sudo install -d -m 0755 /etc/dnsmasq.d

sudo install -m 0644 \
    config/dnsmasq-router.conf \
    /etc/dnsmasq.d/router.conf
```

Install IPv4 forwarding configuration:

```bash
sudo install -m 0644 \
    config/99-router.conf \
    /etc/sysctl.d/99-router.conf

sudo sysctl -p /etc/sysctl.d/99-router.conf
```

Create the bypass-device state directory with an empty device list:

```bash
sudo install -d -m 0755 /etc/proton-bypass

sudo install -m 0644 \
    config/proton-bypass-devices.conf.example \
    /etc/proton-bypass/devices.conf
```

Real device MAC addresses must not be committed to Git.

---

## 8. Install the NetworkManager dispatcher

The same `proton-bypass` executable is used as both the administrator CLI and the NetworkManager dispatcher.

Create the dispatcher directory:

```bash
sudo install -d -m 0755 \
    /etc/NetworkManager/dispatcher.d
```

Remove an old dispatcher copy if present:

```bash
sudo rm -f \
    /etc/NetworkManager/dispatcher.d/90-proton-bypass
```

Create a hardlink:

```bash
sudo ln \
    /usr/local/sbin/proton-bypass \
    /etc/NetworkManager/dispatcher.d/90-proton-bypass
```

Verify that both paths have the same inode:

```bash
stat -c '%i  %U:%G  %a  %n' \
    /usr/local/sbin/proton-bypass \
    /etc/NetworkManager/dispatcher.d/90-proton-bypass
```

The inode numbers must match.

Both files should be:

```text
root:root
755
```

The dispatcher must ignore non-WAN NetworkManager events and return success for them.

A safe test is:

```bash
sudo env NM_DISPATCHER_ACTION=up \
    /etc/NetworkManager/dispatcher.d/90-proton-bypass \
    "$LAN_IF" up

echo $?
```

Expected:

```text
0
```

---

## 9. Install the NetworkManager wait-online override

The ISP may take longer than one DHCP attempt to issue a WAN lease.

The router therefore allows boot to continue once NetworkManager has usable connectivity instead of requiring every startup autoconnect attempt to settle.

Install the override:

```bash
sudo install -d -m 0755 \
    /etc/systemd/system/NetworkManager-wait-online.service.d

sudo install -m 0644 \
    config/NetworkManager-wait-online.override.conf \
    /etc/systemd/system/NetworkManager-wait-online.service.d/override.conf
```

The override should contain:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/nm-online -q --timeout=30
```

Reload systemd:

```bash
sudo systemctl daemon-reload
```

This does not disable WAN DHCP retries. It only prevents slow ISP DHCP from holding `network-online.target` indefinitely.

---

## 10. Install the router-user files

Create the user directories:

```bash
mkdir -p \
    ~/.local/bin \
    ~/.config/systemd/user \
    ~/.config/autostart
```

Install the Proton autoconnect helper:

```bash
install -m 0755 \
    scripts/protonvpn-autoconnect \
    ~/.local/bin/protonvpn-autoconnect
```

Install the Proton user systemd service:

```bash
install -m 0644 \
    config/protonvpn-autoconnect.service \
    ~/.config/systemd/user/protonvpn-autoconnect.service
```

Install the Secret Service desktop autostart entry:

```bash
install -m 0644 \
    config/gnome-keyring-secrets.desktop \
    ~/.config/autostart/gnome-keyring-secrets.desktop
```

Do not enable `protonvpn-autoconnect.service` yet. Proton has not been signed in.

---

## 11. Install the router web interface

The router includes a small single-page web interface for viewing Proton VPN status and managing per-client Proton bypass state.

It uses only Python 3's standard library. No web framework, Node.js runtime, database, nginx, or Apache is required.

Install the web application files:

```bash
sudo install -d -m 0755 \
    /usr/local/share/proton-router-web

sudo install -m 0755 \
    web/server.py \
    /usr/local/share/proton-router-web/server.py

sudo install -m 0644 \
    web/index.html \
    web/app.js \
    web/styles.css \
    /usr/local/share/proton-router-web/
```

Install the user service:

```bash
install -m 0644 \
    config/proton-router-web.service \
    ~/.config/systemd/user/proton-router-web.service
```

The service should contain:

```ini
[Unit]
Description=Proton Router web interface
Wants=gnome-keyring-daemon.service
After=gnome-keyring-daemon.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/share/proton-router-web/server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Do not start or enable the service yet. The generated sudoers policy required for bypass changes is installed by `proton-router-apply-conf` later in this procedure.

The web service listens on TCP port `8080`.

The backend accepts requests only from the configured LAN subnet or loopback and also restricts the accepted Host header to the router LAN IP, `127.0.0.1`, or `localhost`.

The router firewall does not permit WAN clients to access the service.

The web UI intentionally has no separate login screen. Anyone on the trusted LAN who can reach the page can request a bypass change, so do not expose TCP port 8080 through the WAN or an untrusted network.

The backend does not expose a generic shell endpoint. Bypass changes are validated as LAN MAC/IPv4 pairs and can invoke only:

```text
/usr/local/sbin/proton-bypass add <MAC> <IPv4>
/usr/local/sbin/proton-bypass remove <MAC> <IPv4>
```

---

## 12. Remove conflicting LightDM autologin settings

`proton-router-apply-conf` generates:

```text
/etc/lightdm/lightdm.conf.d/90-proton-router-autologin.conf
```

LightDM's main configuration file is read after the drop-in directory, so active autologin settings in `/etc/lightdm/lightdm.conf` would override the generated router configuration.

Back up the original file:

```bash
sudo cp -a \
    /etc/lightdm/lightdm.conf \
    /root/lightdm.conf.pre-proton-router
```

Comment any active autologin user/session values:

```bash
sudo sed -i \
    -e 's/^[[:space:]]*autologin-user=.*/# autologin-user managed by \/etc\/lightdm\/lightdm.conf.d\/90-proton-router-autologin.conf/' \
    -e 's/^[[:space:]]*autologin-session=.*/# autologin-session managed by \/etc\/lightdm\/lightdm.conf.d\/90-proton-router-autologin.conf/' \
    /etc/lightdm/lightdm.conf
```

Do not reboot yet.

---

## 13. Configure NetworkManager

This step can change or interrupt SSH connectivity.

A local keyboard/monitor is recommended for the initial network cutover.

Reload the master configuration if necessary:

```bash
. /etc/proton-router.conf
```

Remove previous project profiles if this is a retry:

```bash
sudo nmcli connection delete LAN 2>/dev/null || true
sudo nmcli connection delete WAN 2>/dev/null || true
```

Create the LAN profile:

```bash
sudo nmcli connection add \
    type ethernet \
    ifname "$LAN_IF" \
    con-name LAN

sudo nmcli connection modify LAN \
    connection.autoconnect yes \
    connection.autoconnect-priority 100 \
    ipv4.method manual \
    ipv4.addresses "$LAN_ADDR/$LAN_PREFIX" \
    ipv4.gateway "" \
    ipv4.never-default yes \
    ipv6.method disabled
```

Create the WAN profile.

For initial setup, allow the ISP-provided DNS temporarily so Proton can be installed and the first router configuration can be completed:

```bash
sudo nmcli connection add \
    type ethernet \
    ifname "$WAN_IF" \
    con-name WAN

sudo nmcli connection modify WAN \
    connection.autoconnect yes \
    connection.autoconnect-priority 100 \
    ipv4.method auto \
    ipv4.never-default no \
    ipv4.ignore-auto-dns no \
    ipv6.method disabled
```

Bring up the WAN:

```bash
sudo nmcli connection up WAN
```

Verify that the ISP has provided a default route:

```bash
ip -4 route show default dev "$WAN_IF"
```

Do not continue until a WAN default route exists.

Bring up the LAN:

```bash
sudo nmcli connection up LAN
```

Verify:

```bash
ip -4 addr show dev "$LAN_IF"
```

The configured router address should be present.

Check all active connections:

```bash
nmcli connection show --active
```

If other Ethernet profiles compete with `LAN` or `WAN`, disable their autoconnect or remove them.

---

## 14. Disable Raspberry Pi Wi-Fi

Do this only after Ethernet administration is working.

If the current SSH connection uses Wi-Fi, it will be disconnected.

Disable the Wi-Fi radio:

```bash
nmcli radio wifi off
```

Verify:

```bash
nmcli radio wifi
```

Expected:

```text
disabled
```

This prevents an unintended alternate Internet path around the router's designated WAN.

---

## 15. Initialize the Proton keyring

This router intentionally uses a dedicated empty-password GNOME login keyring so Proton can retrieve its stored session without an interactive unlock prompt after unattended boot.

Run the initializer as the router user, **not with sudo**:

```bash
/usr/local/sbin/proton-keyring-init
```

On a clean router account, expected output is:

```text
proton-keyring-init: empty-password login keyring initialized
```

Verify:

```bash
test -s ~/.local/share/keyrings/login.keyring &&
    echo "login keyring created"
```

If the initializer instead reports:

```text
proton-keyring-init: existing keyring found; leaving it untouched
```

stop the unattended-login portion of this procedure.

The initializer deliberately refuses to replace or modify an existing keyring because it may contain unrelated secrets. Do not delete an existing keyring blindly.

For clean-room reproducibility testing, use a fresh router account or fresh image if an unexpected keyring already exists.

Never copy the resulting keyring into Git.

---

## 16. Apply the router configuration

Run:

```bash
sudo proton-router-apply-conf
```

A successful run should end with output similar to:

```text
dnsmasq: syntax check OK.
Connection successfully reapplied to device 'eth0'.
proton-bypass synchronized
proton-router configuration applied
```

The apply command generates and validates:

```text
/etc/nftables.d/generated-vars.nft
/etc/dnsmasq.d/generated-vars.conf
/etc/lightdm/lightdm.conf.d/90-proton-router-autologin.conf
/etc/polkit-1/rules.d/49-protonvpn-networkmanager.rules
/etc/sudoers.d/protonvpn-bootstrap
/etc/systemd/system/proton-keyring-init.service
```

It also:

- reapplies the configured LAN address
- restarts dnsmasq
- loads the nftables firewall
- rebuilds the bypass policy-routing state
- reloads systemd
- enables `proton-keyring-init.service`

The generated sudoers policy grants the configured router user passwordless access only to the Proton bootstrap commands and the two `proton-bypass` mutation forms required by the web UI:

```text
/usr/local/sbin/proton-bypass add *
/usr/local/sbin/proton-bypass remove *
```

`proton-bypass` itself requires exactly one MAC address and one IPv4 address for `add` and `remove` and validates both before changing router state.

The web backend is not granted unrestricted root access.

Verify the generated policy:

```bash
sudo visudo -cf \
    /etc/sudoers.d/protonvpn-bootstrap
```

Expected:

```text
/etc/sudoers.d/protonvpn-bootstrap: parsed OK
```

At this point the router's raw-WAN host firewall is fail-closed.

---

## 17. Test and enable the router web interface

Reload the router user's systemd manager:

```bash
systemctl --user daemon-reload
```

Start the web service for the first test:

```bash
systemctl --user start \
    proton-router-web.service
```

Wait briefly for Python to bind the socket:

```bash
sleep 2
```

Check its status:

```bash
systemctl --user status \
    proton-router-web.service \
    --no-pager -l
```

Expected:

```text
Active: active (running)
```

Verify the listening socket:

```bash
ss -ltn |
    grep ':8080'
```

Expected output should show a listener on:

```text
0.0.0.0:8080
```

Test both read-only API endpoints using Python's standard library:

```bash
python3 - <<'PY'
import json
import urllib.request

for path in ("/api/status", "/api/clients"):
    with urllib.request.urlopen(
        "http://127.0.0.1:8080" + path,
        timeout=10,
    ) as response:
        payload = json.load(response)
        print(
            path,
            response.status,
            "OK" if payload.get("ok") else payload,
        )
PY
```

Expected:

```text
/api/status 200 OK
/api/clients 200 OK
```

Test the static assets:

```bash
python3 - <<'PY'
import urllib.request

for path in ("/", "/app.js", "/styles.css"):
    with urllib.request.urlopen(
        "http://127.0.0.1:8080" + path,
        timeout=5,
    ) as response:
        body = response.read()
        print(
            path,
            response.status,
            len(body),
            response.headers.get("Content-Type"),
        )
PY
```

Each path should return HTTP `200`.

Load the master configuration and print the browser URL:

```bash
. /etc/proton-router.conf
printf 'Open: http://%s:8080/\n' "$LAN_ADDR"
```

Open that URL from a device on the LAN.

With the default configuration:

```text
http://192.168.50.1:8080/
```

The page should show:

- Proton VPN connection state
- Proton server, protocol, and load when available
- WAN readiness and current gateway
- configured LAN address/interface
- known LAN clients discovered from the neighbor table and dnsmasq leases
- whether each client currently uses Proton or raw-WAN bypass
- a button to add or remove bypass for known MAC/IP pairs

Before enabling automatic startup, verify that the sudoers policy contains only the intended web mutation commands:

```bash
sudo grep -E \
    'PROTONROUTER_WEB|NOPASSWD' \
    /etc/sudoers.d/protonvpn-bootstrap
```

It should include:

```text
Cmnd_Alias PROTONROUTER_WEB = /usr/local/sbin/proton-bypass add *, /usr/local/sbin/proton-bypass remove *
```

A safe direct sudo test is to re-add a device that is already configured for bypass:

```bash
sudo -n /usr/local/sbin/proton-bypass \
    add aa:bb:cc:dd:ee:ff 192.168.50.180
```

Use a MAC/IP pair that is already present in `sudo proton-bypass list`.

The command should return success without changing the desired routing state.

For a full web API round-trip test, temporarily remove and then restore an already-bypassed device:

```bash
python3 - <<'PY'
import json
import urllib.request

URL = "http://127.0.0.1:8080/api/bypass"

device = {
    "mac": "aa:bb:cc:dd:ee:ff",
    "ip": "192.168.50.180",
}

body = json.dumps(device).encode("utf-8")

def request(method):
    req = urllib.request.Request(
        URL,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )

    with urllib.request.urlopen(
        req,
        timeout=10,
    ) as response:
        payload = json.load(response)
        print(method, response.status, payload)

request("DELETE")
request("POST")
PY
```

Replace the example MAC/IP with a device that is already configured for bypass.

The final `POST` must restore the original bypass state.

Enable the web service:

```bash
systemctl --user enable \
    proton-router-web.service
```

Verify:

```bash
systemctl --user is-enabled \
    proton-router-web.service

systemctl --user is-active \
    proton-router-web.service
```

Expected:

```text
enabled
active
```

Because this is a user service, it starts automatically with the router user's unattended desktop login.

---

## 18. Point the router itself at dnsmasq

Now that dnsmasq is configured and running, prevent the WAN DHCP lease from installing ISP DNS servers.

Run:

```bash
sudo nmcli connection modify WAN \
    ipv4.ignore-auto-dns yes \
    ipv4.dns "127.0.0.1" \
    ipv6.method disabled
```

Reapply the active WAN profile:

```bash
sudo nmcli device reapply "$WAN_IF"
```

If reapply is unavailable for some reason:

```bash
sudo nmcli connection up WAN
```

Verify:

```bash
grep '^nameserver' /etc/resolv.conf
```

Expected:

```text
nameserver 127.0.0.1
```

---

## 19. Enable system services

Enable the required boot services:

```bash
sudo systemctl enable \
    NetworkManager.service \
    NetworkManager-wait-online.service \
    nftables.service \
    dnsmasq.service \
    lightdm.service
```

Verify that the generated keyring service was enabled by the apply command:

```bash
systemctl is-enabled \
    proton-keyring-init.service
```

Expected:

```text
enabled
```

Do not enable Proton autoconnect yet.

The router web service may already be enabled from the previous section; that is fine.

---

## 20. First reboot

Reboot:

```bash
sudo reboot
```

The expected boot sequence is:

1. NetworkManager starts LAN and begins WAN DHCP.
2. The router does not require every WAN DHCP autoconnect attempt to settle before local boot can continue.
3. `proton-keyring-init.service` runs before LightDM.
4. LightDM automatically logs in `ROUTER_USER`.
5. the desktop starts GNOME Secret Service.
6. nftables and dnsmasq are active.
7. the web interface starts with the router user's session.
8. Proton remains disconnected because Proton autoconnect has not yet been enabled.

After logging back in, verify the Secret Service exists:

```bash
busctl --user --list |
    grep '^org.freedesktop.secrets'
```

Do not continue until that service is present.

Verify the web service:

```bash
systemctl --user is-enabled \
    proton-router-web.service

systemctl --user is-active \
    proton-router-web.service
```

Expected:

```text
enabled
active
```

---

## 21. Sign in to Proton once

The router firewall now blocks ordinary Pi-originated raw-WAN traffic.

For the one-time Proton sign-in, temporarily open the dedicated bootstrap chain.

First make sure it is closed:

```bash
sudo nft flush chain \
    inet router_filter host_wan_bootstrap
```

Open it:

```bash
sudo nft add rule \
    inet router_filter host_wan_bootstrap accept
```

Sign in:

```bash
protonvpn signin YOUR_PROTON_USERNAME
```

Enter the Proton account password when prompted.

Configure the tested router settings:

```bash
protonvpn config set kill-switch standard
protonvpn config set ipv6 off
```

Verify:

```bash
protonvpn config list
```

The important values are:

```text
kill-switch    standard
ipv6           off
```

Immediately close the temporary bootstrap gate:

```bash
sudo nft flush chain \
    inet router_filter host_wan_bootstrap
```

Do not leave this chain open.

Proton credentials and session information are stored in the user's keyring and must never be committed to Git.

---

## 22. Test the managed Proton connection

Do not use bare `protonvpn connect` for normal router operation.

The router's fail-closed firewall intentionally prevents an unwrapped Proton connection attempt from using unrestricted raw WAN.

Instead run:

```bash
~/.local/bin/protonvpn-autoconnect
```

This helper:

1. reads `WAN_IF` from `/etc/proton-router.conf`
2. waits for the WAN to have an IPv4 default route
3. waits for Secret Service
4. confirms Proton is not already connected
5. temporarily opens the host bootstrap firewall
6. runs `protonvpn connect`
7. closes the bootstrap firewall on both success and failure

Verify:

```bash
protonvpn status
```

Expected:

```text
Status: Connected
```

The actual free Proton server can change dynamically.

The web UI should now reflect the live Proton state within its normal refresh interval.

---

## 23. Enable Proton autoconnect

Reload the router user's systemd manager:

```bash
systemctl --user daemon-reload
```

Enable the service:

```bash
systemctl --user enable \
    protonvpn-autoconnect.service
```

Verify:

```bash
systemctl --user is-enabled \
    protonvpn-autoconnect.service
```

Expected:

```text
enabled
```

The service can appear `inactive` after a successful connection. This is normal: the helper exits successfully after Proton is connected, and the unit only restarts on failure.

---

## 24. Final unattended reboot test

Reboot again:

```bash
sudo reboot
```

Do not manually connect Proton after this reboot.

The purpose of this reboot is to prove unattended operation.

After the system is back, verify:

```bash
echo '=== CORE SERVICES ==='

for s in \
    NetworkManager.service \
    NetworkManager-wait-online.service \
    nftables.service \
    dnsmasq.service \
    lightdm.service \
    proton-keyring-init.service
do
    printf '%-38s enabled=%-10s active=%s\n' \
        "$s" \
        "$(systemctl is-enabled "$s" 2>/dev/null || true)" \
        "$(systemctl is-active "$s" 2>/dev/null || true)"
done
```

`NetworkManager-wait-online`, nftables, dnsmasq, LightDM, and the keyring initializer should not be failed.

Check the Proton user service:

```bash
systemctl --user is-enabled \
    protonvpn-autoconnect.service
```

Expected:

```text
enabled
```

Check the router web service:

```bash
systemctl --user is-enabled \
    proton-router-web.service

systemctl --user is-active \
    proton-router-web.service
```

Expected:

```text
enabled
active
```

Because the Python service can require a short moment to bind port 8080 after user login, wait briefly before testing it:

```bash
sleep 2
```

Test the local web API:

```bash
python3 - <<'PY'
import json
import urllib.request

with urllib.request.urlopen(
    "http://127.0.0.1:8080/api/status",
    timeout=5,
) as response:
    payload = json.load(response)
    print(
        "web-api=OK"
        if payload.get("ok")
        else payload
    )
PY
```

Expected:

```text
web-api=OK
```

Check Proton:

```bash
protonvpn status
```

Expected:

```text
Status: Connected
```

Check Wi-Fi:

```bash
nmcli radio wifi
```

Expected:

```text
disabled
```

Check forwarding:

```bash
sysctl net.ipv4.ip_forward
```

Expected:

```text
net.ipv4.ip_forward = 1
```

Check local DNS:

```bash
grep '^nameserver' /etc/resolv.conf
```

Expected:

```text
nameserver 127.0.0.1
```

Check the bypass policy rule:

```bash
ip -4 rule show |
    grep '10000:'
```

With the default configuration it should refer to:

```text
fwmark 0x50564250
lookup 200
```

Check the bypass routing table:

```bash
ip -4 route show table 200
```

It should contain:

- the LAN connected route
- the current WAN connected route when WAN is available
- the current WAN default gateway when WAN is available
- a blackhole default route for fail-closed behavior

From another LAN client, open:

```text
http://<LAN_ADDR>:8080/
```

With the default configuration:

```text
http://192.168.50.1:8080/
```

Verify that Proton and client state are visible after unattended boot.

---

## 25. Verify generated configuration

Check LightDM:

```bash
sudo grep -RniE \
    '^[[:space:]]*(autologin-user|autologin-session)[[:space:]]*=' \
    /etc/lightdm/lightdm.conf \
    /etc/lightdm/lightdm.conf.d \
    2>/dev/null
```

The only active values should come from:

```text
/etc/lightdm/lightdm.conf.d/90-proton-router-autologin.conf
```

Check sudoers:

```bash
sudo visudo -cf \
    /etc/sudoers.d/protonvpn-bootstrap
```

Expected:

```text
parsed OK
```

Check the NetworkManager dispatcher:

```bash
stat -c '%i  %U:%G  %a  %n' \
    /usr/local/sbin/proton-bypass \
    /etc/NetworkManager/dispatcher.d/90-proton-bypass
```

The inode numbers must match.

Check nftables syntax:

```bash
sudo nft -c -f /etc/nftables.conf
```

Check dnsmasq syntax:

```bash
sudo dnsmasq --test
```

Expected:

```text
dnsmasq: syntax check OK.
```

Check the web service listener:

```bash
ss -ltn |
    grep ':8080'
```

Check both web API endpoints:

```bash
python3 - <<'PY'
import json
import urllib.request

for path in ("/api/status", "/api/clients"):
    with urllib.request.urlopen(
        "http://127.0.0.1:8080" + path,
        timeout=5,
    ) as response:
        payload = json.load(response)
        print(path, response.status, payload.get("ok"))
PY
```

Expected:

```text
/api/status 200 True
/api/clients 200 True
```

---

## 26. Connect the downstream access point

Connect:

```text
ISP modem
    |
    v
WAN interface on Raspberry Pi
    |
Raspberry Pi
    |
LAN interface
    |
    v
access point / switch
    |
    v
clients
```

The wireless router or access point downstream of the Pi should operate in **access point / bridge mode**, not as another NAT router.

A normal DHCP client should receive:

- an address from the configured DHCP range
- the Pi LAN address as its default gateway
- the Pi LAN address as its DNS server

With the default example configuration:

```text
address: 192.168.50.100 - 192.168.50.220
gateway: 192.168.50.1
DNS:     192.168.50.1
```

---

## 27. Add optional raw-WAN bypass devices

No real bypass devices are included in Git.

Add one using its MAC address and desired reserved LAN address:

```bash
sudo proton-bypass add \
    aa:bb:cc:dd:ee:ff \
    192.168.50.180
```

List configured devices:

```bash
sudo proton-bypass list
```

Remove one:

```bash
sudo proton-bypass remove \
    aa:bb:cc:dd:ee:ff \
    192.168.50.180
```

The device will receive the specified address through dnsmasq and will be policy-routed directly to the ISP WAN.

DNS and firewall enforcement still apply.

The same operations can be performed from the local router web UI:

```text
http://<LAN_ADDR>:8080/
```

The web UI only allows bypass mutations for MAC/IP pairs already known to the router.

---

## 28. Reproducibility success criteria

A fresh build is considered successful when all of the following are true:

- Raspberry Pi boots without manual intervention.
- LAN is available even if the ISP is slow to issue a DHCP lease.
- WAN DHCP can recover dynamically without hardcoded ISP addresses.
- `NetworkManager-wait-online.service` does not fail merely because WAN DHCP is still retrying.
- IPv4 forwarding is enabled.
- Wi-Fi is disabled.
- dnsmasq supplies DHCP and DNS to LAN clients.
- `/etc/resolv.conf` uses `127.0.0.1`.
- nftables is active.
- normal client traffic cannot use the raw WAN.
- Proton reconnects automatically after boot.
- Proton credentials do not require an interactive keyring unlock.
- `protonvpn status` reports connected after unattended boot.
- bypass routing contains a blackhole default whenever usable WAN routing is unavailable.
- configured bypass devices use the current dynamic ISP gateway.
- non-WAN NetworkManager dispatcher events exit successfully.
- LightDM autologin values are generated from `/etc/proton-router.conf`.
- polkit, sudoers, dnsmasq variables, nftables variables, and the keyring service are generated rather than hand-edited.
- `proton-router-web.service` is enabled and active after unattended login.
- `http://<LAN_ADDR>:8080/` loads from a LAN client without additional packages or a separate web server.
- `/api/status` reports Proton and WAN state.
- `/api/clients` reports known LAN clients and current bypass state.
- a bypass add/remove initiated from the page is executed through `proton-bypass`, not through a general-purpose shell endpoint.
- the web service is unreachable from the WAN.
- the generated sudoers policy grants the router user only the Proton bootstrap commands and the two required `proton-bypass` mutation forms.
- no credentials, private keys, real keyring files, or real bypass-device identities are present in the Git repository.

If any one of these fails during a clean-room build, record the failing step before modifying the procedure.

That failure identifies either:

- a missing dependency
- hidden machine state
- a boot-order dependency
- hardware-specific interface behavior
- or configuration that still needs to be made portable

---

## 29. Files that must never be copied from an existing router

Do not use an existing configured router as a source for these files:

```text
/etc/wireguard/proton0.conf
~/.local/share/keyrings/*
/etc/proton-bypass/devices.conf
```

Also do not copy machine-generated files from an existing router:

```text
/etc/nftables.d/generated-vars.nft
/etc/dnsmasq.d/generated-vars.conf
/etc/lightdm/lightdm.conf.d/90-proton-router-autologin.conf
/etc/polkit-1/rules.d/49-protonvpn-networkmanager.rules
/etc/sudoers.d/protonvpn-bootstrap
/etc/systemd/system/proton-keyring-init.service
```

Those files must be recreated on the new router by:

```bash
sudo proton-router-apply-conf
```

This distinction is central to the reproducibility model:

- Git stores source configuration and logic.
- `proton-router-apply-conf` generates machine-specific system configuration.
- Proton and GNOME Keyring maintain authentication/session secrets locally.
- dnsmasq and NetworkManager maintain runtime client/network state locally.
- the web UI reads runtime state but does not become a second configuration database.
