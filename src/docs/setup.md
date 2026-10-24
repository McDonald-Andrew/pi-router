# Fresh Raspberry Pi Setup

This document builds the router from a fresh Raspberry Pi OS installation using the same Raspberry Pi setup phases driven by the Windows `Program.cs` installer.

The `setup` script is the canonical implementation. The Windows installer automates its phases; Linux users following this document run those same phases manually. The goal is a reproducibility test: a fresh Raspberry Pi should arrive at the same final router configuration, including the local web management interface.

## 1. Starting point

Tested hardware:

- Raspberry Pi 5
- built-in Raspberry Pi Ethernet used as `eth0` / LAN
- second Ethernet adapter used as `eth1` / WAN
- Linux setup computer connected by Ethernet to the Pi built-in RJ45
- upstream router LAN connected to the Pi second Ethernet adapter
- Raspberry Pi initially reachable over Wi-Fi for SSH

Tested software baseline:

- Raspberry Pi OS Desktop, 64-bit
- Debian 13 / trixie
- NetworkManager
- LightDM
- `rpd-labwc` desktop session
- Proton VPN Linux CLI installed by the canonical setup flow

Raspberry Pi OS Desktop is recommended rather than Raspberry Pi OS Lite because this design uses:

- LightDM autologin
- a desktop user session
- GNOME Secret Service / GNOME Keyring
- the Proton VPN Linux CLI
- user-level systemd services for Proton autoconnect and the router web interface

> Proton does not officially support Raspberry Pi OS/Raspbian. This project uses the Debian ARM64 packages and has been tested successfully on Raspberry Pi OS Desktop, but this is not an officially supported Proton platform.

Do not put Proton credentials, keyrings, WireGuard private keys, or real bypass-device identities into this repository or into shell scripts.

---

## 2. Install Raspberry Pi OS

Flash a fresh 64-bit Raspberry Pi OS Desktop image.

During initial setup, create the account that will own the Proton VPN session. The repository example uses:

```text
admin
```

A different username is supported as long as it is the router account used by the setup flow.

Before beginning the router installation:

1. Enable SSH.
2. Connect the Pi to Wi-Fi so the Linux setup computer can reach it initially.
3. Do not manually assign a static address to either Pi Ethernet interface.
4. Leave the Linux setup computer's Pi-facing Ethernet interface configured for automatic DHCP.
5. Connect the physical Ethernet topology described in Section 4.

To match the automated installer flow, do not manually pre-install Proton, create NetworkManager `LAN`/`WAN` profiles, edit nftables, edit dnsmasq, initialize the Proton keyring, or enable the router services. Those operations belong to the canonical `setup` phases.

An OS update performed before this procedure is outside the installer parity path. If you choose to update the base OS first and the update installs a new kernel, reboot before beginning Section 3.

---

## 3. Place and verify the certified router release

For parity with the automated installer, use the release archive rather than manually copying an already-configured router tree.

Place these two files in the router user's home directory:

```text
~/proton-router-v0.0.0.tar.gz # Replace with the release version, for example 0.1.0
~/proton-router-v0.0.0.sha256.txt # Replace with the release version, for example 0.1.0
```

Verify that the archive hash matches the hash recorded in the sidecar:

```bash
cd ~

release="proton-router-v0.0.0" # Replace with the release version, for example 0.1.0
archive_sha="$(sha256sum "$release.tar.gz" | awk '{print $1}')"
sidecar_sha="$(grep -Eio '[0-9a-f]{64}' "$release.sha256.txt" | head -n 1 | tr '[:lower:]' '[:upper:]')"
archive_sha_upper="$(printf '%s' "$archive_sha" | tr '[:lower:]' '[:upper:]')"

test -n "$sidecar_sha" && \
    test "$archive_sha_upper" = "$sidecar_sha" && \
    echo "archive-sha256=PASS"
```

If `archive-sha256=PASS` is not printed, stop.

For release authenticity, the sidecar hash should also be compared with the hash published for the release. The Windows installer performs an additional compiled-in SHA-256 certification check that is Windows-installer-specific.

Extract a clean copy:

```bash
cd ~
release="proton-router-v0.0.0" # Replace with the release version, for example 0.1.0
rm -rf "$HOME/$release"

tar -xzf "$HOME/$release.tar.gz"
chmod +x "$HOME/$release/setup"
```

Enter the release directory:

```bash
cd ~/proton-router-v0.0.0 # Replace with the release version, for example 0.1.0
```

Verify the setup entry point:

```bash
./setup --help >/dev/null &&
    echo "setup entry point OK"
```

Expected:

```text
setup entry point OK
```

All remaining relative paths in this guide assume the release exists at:

```text
~/proton-router-vX.Y.Z
```

---

## 4. Verify the physical topology and Pi interfaces

On the Pi, inspect the available interfaces:

```bash
ip -br link
```

The certified/tested topology used by the automated installer is:

```text
Linux setup computer Ethernet -> built-in Pi RJ45 -> eth0 -> LAN
upstream router LAN            -> second Pi RJ45   -> eth1 -> WAN
Pi Wi-Fi                       -> initial SSH management path
```

Both Ethernet cables must be connected before installation.

Verify carrier exactly as the automated installer does:

```bash
printf 'eth0='
cat /sys/class/net/eth0/carrier
printf 'eth1='
cat /sys/class/net/eth1/carrier
```

Expected:

```text
eth0=1
eth1=1
```

Do not continue until both links report carrier.

The current automated installer assumes `eth0` is LAN and `eth1` is WAN on the tested hardware. If different hardware produces different interface names, this document is no longer an exact reproduction of the current `Program.cs` flow without corresponding project changes.

Make sure the Linux setup computer can still SSH to the Pi over Wi-Fi before starting Section 5.

---

## 5. Run the canonical pre-reboot phase over Wi-Fi

From the Linux setup computer, SSH to the fresh Pi using its Wi-Fi hostname or address:

```bash
ssh <ROUTER_USER>@<PI_WIFI_HOST>
```

Enter the release directory:

```bash
cd ~/proton-router-v0.0.0 # Replace with the release version, for example 0.1.0
```

Run the same pre-reboot phase used by the automated installer:

```bash
./setup \
    --live \
    --allow-live-install \
    --phase pre-reboot \
    --allow-network-cutover \
    --allow-wifi-disable \
    --non-interactive
```

This phase owns the package installation, project file installation, initial NetworkManager configuration, keyring preparation, boot-service preparation, firewall/dnsmasq configuration, temporary provisioning DHCP state, and Wi-Fi-to-LAN management handoff.

There are two valid outcomes.

If the phase prints:

```text
pre-reboot stage complete
REBOOT REQUIRED before post-reboot
```

the entire pre-reboot phase completed without requiring a separate LAN handoff. Skip directly to Section 8.

When setup is being performed through Wi-Fi SSH, the normal expected outcome is that setup stops at the management handoff gate after messages including:

```text
provisioning DHCP enabled
refusing to disable Wi-Fi
```

If those messages are reached, continue with Section 6.

Do not reboot yet.

---

## 6. Move the Linux setup computer from Wi-Fi management to Pi LAN

The Pi is now temporarily providing provisioning DHCP on its LAN interface.

The Linux setup computer's Ethernet interface must remain configured for automatic DHCP. Do not assign a static address.

If the Linux setup computer uses NetworkManager, identify the Ethernet interface connected directly to the Pi:

```bash
nmcli device status
```

Replace `<CLIENT_ETH_IF>` below with that interface and cycle it once:

```bash
nmcli device disconnect <CLIENT_ETH_IF>
nmcli device connect <CLIENT_ETH_IF>
```

Inspect the resulting IPv4 state:

```bash
nmcli -g \
    IP4.ADDRESS,IP4.GATEWAY,IP4.DNS \
    device show <CLIENT_ETH_IF>
```

During the provisioning stage, the Ethernet interface must receive an address in:

```text
192.168.50.0/24
```

For example:

```text
192.168.50.100/24
```

At this stage the Pi-facing Ethernet connection should have no default gateway and no DNS server supplied by the Pi.

Connect to the Pi over LAN:

```bash
ssh <ROUTER_USER>@192.168.50.1
```

Do not continue until LAN SSH works.

---

## 7. Resume the pre-reboot phase over LAN

After reconnecting to the Pi at `192.168.50.1`, enter the release directory:

```bash
cd ~/proton-router-v0.0.0 # Replace with the release version, for example 0.1.0
```

Remove any checkpoint left by an earlier incomplete installation attempt:

```bash
sudo rm -f \
    /var/lib/proton-router-setup/reboot-required.boot-id
```

Run the same LAN resume phase used by the automated installer:

```bash
./setup \
    --live \
    --allow-live-install \
    --phase pre-reboot-resume \
    --allow-network-cutover \
    --allow-wifi-disable \
    --non-interactive
```

Successful completion must reach:

```text
pre-reboot stage complete
REBOOT REQUIRED before post-reboot
```

### If SSH disconnects near the end of the resume phase

Reconnect to the Pi over LAN:

```bash
ssh <ROUTER_USER>@192.168.50.1
```

Then verify the same reboot checkpoint that the automated installer trusts after a late SSH disconnect:

```bash
test "$(
    sudo cat \
        /var/lib/proton-router-setup/reboot-required.boot-id \
        2>/dev/null
)" = "$(
    cat /proc/sys/kernel/random/boot_id
)" &&
    echo "PRE_REBOOT_CHECKPOINT_OK"
```

If this prints:

```text
PRE_REBOOT_CHECKPOINT_OK
```

the pre-reboot phase completed successfully even though the SSH transport disappeared before its final output was received.

If it does not print that message, do not reboot. Re-run `pre-reboot-resume` and diagnose the failed gate first.

---

## 8. Perform the first reboot

Only reboot after the pre-reboot phase has reached its reboot checkpoint.

Run:

```bash
sudo reboot
```

The Ethernet carrier can return before the Pi DHCP service is fully ready. Give the Pi time to boot, then renew the Linux setup computer's wired DHCP lease by reconnecting the interface if necessary:

```bash
nmcli device disconnect <CLIENT_ETH_IF>
nmcli device connect <CLIENT_ETH_IF>
```

The setup computer should again obtain a `192.168.50.x` address.

Reconnect:

```bash
ssh <ROUTER_USER>@192.168.50.1
```

Do not manually connect Proton at this point.

---

## 9. Run the canonical post-reboot phase

Enter the release directory:

```bash
cd ~/proton-router-v0.0.0" # Replace with the release version, for example 0.1.0
```

Run the same post-reboot phase used by the automated installer:

```bash
./setup \
    --live \
    --allow-live-install \
    --phase post-reboot
```

When prompted:

```text
Proton account username or email:
```

enter the Proton account username or email.

When prompted for the Proton password, enter it normally.

Do not place Proton credentials in shell history, configuration files, scripts, or Git.

The setup phase owns the temporary Proton bootstrap firewall opening and closing, Proton sign-in/configuration, managed connection validation, and unattended service enablement.

Do not manually add an unrestricted bootstrap firewall rule and do not use bare:

```bash
protonvpn connect
```

for normal router operation.

The dynamic VPN endpoint refresh infrastructure is installed and its timer is enabled, but the initial endpoint database build is intentionally deferred. Building that database is not an installation gate.

Successful completion must include:

```text
Proton managed connection verified
proton0 WireGuard interface verified
post-reboot stage complete
FINAL REBOOT REQUIRED for unattended verification
```

Do not continue to the final reboot unless those checks complete.

---

## 10. Recover from a post-reboot SSH interruption

The Proton/NetworkManager transition can occasionally interrupt an SSH session even when the post-reboot work has continued successfully.

If SSH disconnects while Section 9 is running, reconnect:

```bash
ssh <ROUTER_USER>@192.168.50.1
```

Enter the release directory again:

```bash
cd ~/proton-router-v0.0.0 # Replace with the release version, for example 0.1.0
```

Re-run the same post-reboot phase:

```bash
./setup \
    --live \
    --allow-live-install \
    --phase post-reboot
```

Continue only after it reaches:

```text
post-reboot stage complete
FINAL REBOOT REQUIRED for unattended verification
```

---

## 11. Refresh the Linux client's DHCP lease after router promotion

After the post-reboot phase succeeds, the LAN has been promoted from temporary provisioning DHCP to normal router DHCP.

Keep the Linux setup computer's Ethernet interface in automatic DHCP mode and reconnect it:

```bash
nmcli device disconnect <CLIENT_ETH_IF>
nmcli device connect <CLIENT_ETH_IF>
```

Inspect the resulting configuration:

```bash
nmcli -g \
    IP4.ADDRESS,IP4.GATEWAY,IP4.DNS \
    device show <CLIENT_ETH_IF>
```

The Pi-facing Ethernet connection must now have:

```text
IPv4 address: 192.168.50.x
gateway:      192.168.50.1
DNS:          192.168.50.1
```

The exact client address may vary.

Do not manually assign a static address.

---

## 12. Perform the final unattended reboot

Reconnect to the Pi if necessary:

```bash
ssh <ROUTER_USER>@192.168.50.1
```

Reboot:

```bash
sudo reboot
```

Do not manually run Proton after this reboot.

This reboot proves that the complete router stack can start without interactive assistance.

After the Pi begins returning, renew the Linux setup computer's Ethernet DHCP lease if necessary and reconnect to `192.168.50.1`.

---

## 13. Wait for unattended Proton connection

Reconnect to the Pi:

```bash
ssh <ROUTER_USER>@192.168.50.1
```

Give Proton up to three minutes to connect automatically, matching the automated installer's unattended-Proton gate:

```bash
for _ in $(seq 1 60); do
    if protonvpn status 2>/dev/null |
        grep -q '^Status: Connected'
    then
        break
    fi

    sleep 3
done
```

Verify:

```bash
protonvpn status
```

Expected:

```text
Status: Connected
```

Enforce the result:

```bash
protonvpn status 2>/dev/null |
    grep -q '^Status: Connected' ||
    {
        echo "FAIL: Proton did not autoconnect within 3 minutes"
        exit 1
    }
```

---

## 14. Run the canonical final verification

Enter the release directory:

```bash
cd ~/proton-router-v0.0.0 # Replace with the release version, for example 0.1.0
```

Run exactly the same final verification phase used by the automated installer:

```bash
./setup \
    --live \
    --allow-live-install \
    --phase final-verify \
    --non-interactive
```

Installation is not considered complete unless this command succeeds and prints:

```text
FINAL VERIFICATION PASSED
```

If this phase fails, preserve its output and diagnose the failed gate instead of manually forcing the router into service.

---

## 15. Installation complete

At this point the Linux manual installation has followed the same Raspberry Pi phase order and reboot boundaries used by `Program.cs`:

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

The router should now be administered through the LAN.

With the default LAN configuration, the web interface is available at:

```text
http://192.168.50.1:8080/
```

Do not replace the phase-based installation procedure with individually copied package, NetworkManager, nftables, dnsmasq, keyring, Proton, or systemd commands. Those implementation details belong to the `setup` script so that the Windows installer and Linux manual procedure cannot silently drift apart.

---

## 16. Optional detailed verification

The canonical installation success gate is:

```bash
cd ~/proton-router-v0.0.0 # Replace with the release version, for example 0.1.0

./setup \
    --live \
    --allow-live-install \
    --phase final-verify \
    --non-interactive
```

A successful installation must print:

```text
FINAL VERIFICATION PASSED
```

The checks below are additional diagnostics and inspection commands. They do not replace the canonical `final-verify` phase.

Verify that the deferred VPN endpoint refresh timer is installed and enabled:

```bash
systemctl is-enabled proton-router-vpn-refresh.timer
```

Expected:

```text
enabled
```

The endpoint database itself is intentionally not required to exist during installation. It is built later by the scheduled timer or by an explicit manual/web refresh.

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

## 17. Connect the downstream access point

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

## 18. Add optional raw-WAN bypass devices

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

## 19. Reproducibility success criteria

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
- `proton-router-vpn-refresh.timer` is installed and enabled.
- installation does not require an initial dynamic VPN endpoint database build to complete successfully.
- the dynamic VPN endpoint database can be built later by the scheduled timer or by an explicit manual/web refresh.

If any one of these fails during a clean-room build, record the failing step before modifying the procedure.

That failure identifies either:

- a missing dependency
- hidden machine state
- a boot-order dependency
- hardware-specific interface behavior
- or configuration that still needs to be made portable

---

## 20. Files that must never be copied from an existing router

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

Those files are recreated by `proton-router-apply-conf`, which the canonical `setup` phases invoke as part of installation. For manual recovery or diagnostics, the underlying command is:

```bash
sudo proton-router-apply-conf
```

Do not substitute that one command for the phase-based installation procedure.

This distinction is central to the reproducibility model:

- Git stores source configuration and logic.
- `proton-router-apply-conf` generates machine-specific system configuration.
- Proton and GNOME Keyring maintain authentication/session secrets locally.
- dnsmasq and NetworkManager maintain runtime client/network state locally.
- the web UI reads runtime state but does not become a second configuration database.
