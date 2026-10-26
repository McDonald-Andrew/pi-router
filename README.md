<div align="center">
  <img src="src/web/favicon.svg" width="128" alt="Pi Router shield logo">
  <h1>Pi Router</h1>
  <p>The router for families who want a safer, more secure home internet without sacrificing privacy</p>
</div>

# How it Works

- The Raspberry Pi 5 router applies firewall and domain policies that block listed web proxies, Tor relays, remote browsers, VPN endpoints, and others before their lookups can leave the router.
- A local Proton VPN tunnel (account required) is used to forward DNS requests to a Cloudflare Families DNS resolver, where Cloudflare sees the VPN exit address, not the household's public IP.
- Traffic that is permitted by the firewall and domain policies is referred to as "permitted traffic".
- Permitted application traffic from normal devices exits through Proton VPN, while permitted application traffic from devices using the application-traffic bypass uses the ISP connection without bypassing the Pi’s DNS, domain, or firewall policies.
- When Proton VPN is unavailable, normal devices cannot access the WAN, while bypassed devices retain WAN connectivity, but generally cannot reach new uncached domains since the Pi cannot complete upstream DNS resolution.
- However, if Proton VPN is attempting to connect, the router creates a temporary gate that permits DNS requests through the ISP, causing Cloudflare to see the household's public IP as well as temporarily allowing new permitted domains to resolve, but this gate closes when each connection process succeeds or fails.

# Required Contents

- Raspberry Pi 5
- Waveshare PCIe to Gigabit Ethernet Board
- micro SD card
- 2x Ethernet cables

After putting the Pi5 router together, install the latest Raspberry Pi OS Desktop (x64) onto the micro SD card and insert.

Ensure an ethernet cable is connected from your machine to the Pi 5's built-in ethernet port (`eth0`). The other ethernet cable is connected from one of your current router's LAN ports to the Waveshare PCIe device's ethernet port (`eth1`).

# Installing `pi-router`

- If using Windows, it is recommended to use the Windows installer packaged in with the latest release.
- A built or downloaded release `*.tar.gz` and `*.sha256.txt` is needed in the same folder as the `*.exe` Windows installer to run it.
- To build `*.tar.gz` and `*.sha256.txt` files, see [Pack / Repack `pi-router-vX.Y.Z.tar.gz`](#pack--repack-pi-router-vxyz-targz) below.

- To build a Windows installer, follow [windows-installer/BUILD.md](windows-installer/BUILD.md).
  - The installer **_may fail_** on various steps due to either ethernet or ssh disconnects. If this happens, retry the installation. If this still does not work, **re-image the Pi 5 with a fresh Raspberry Pi OS Desktop image** and start again.
- Otherwise, or if using Linux, you may run the setup script steps manually and follow `src/docs/setup.md`.
  - A built or downloaded release `*.tar.gz` and `*.sha256.txt` is still needed for the manual setup.

# Final Setup

Once installation is complete you may follow the final steps for setup:

1. disconnect the ethernet cables and place the Pi in this orientation
2. Switch your internet router to Access Point/AP mode.
3. The **final** topology is as follows:

- AP LAN port -> Pi built-in eth0
- ISP modem WAN port -> Pi Waveshare PCIe eth1

4. You may turn off/unplug the router, Pi, and modem
5. Let the modem sit for 1-2mins before plugging back in and connecting to the Pi. This allows the modem and Pi to recognize one another.

6. Turn on the Pi and router.

Congratulations! Your Pi Router is finished!

# Pack / Repack `pi-router-vX.Y.Z.tar.gz`

Create a new archive:

```powershell
$parent = "$env:USERPROFILE\Desktop\GitHubRepos\pi-router"
$release = "pi-router-vX.Y.Z" # Replace with the release version, for example 0.1.0
$releaseDir = "$parent\$release"
$out = "$parent\$release.tar.gz"

Remove-Item $out, $releaseDir -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item "$parent\src" $releaseDir -Recurse

tar -czf $out -C $parent $release
Remove-Item $releaseDir -Recurse -Force
```

Generate new SHA256 text file for verification:

```powershell
$newSha = "$parent\$release.sha256.txt"

(Get-FileHash $out -Algorithm SHA256).Hash | Set-Content $newSha
```

Verify:

```powershell
$expected = (Get-Content $newSha).Trim().ToUpper()
$actual = (Get-FileHash $out -Algorithm SHA256).Hash.ToUpper()

"Expected: $expected"
"Actual:   $actual"

if ($expected -eq $actual) {
    "ARCHIVE VERIFY: PASS"
} else {
    "ARCHIVE VERIFY: FAIL"
}
```

# Unpack `pi-router-vX.Y.Z.tar.gz`

```powershell
$tarFile="pi-router-vX.Y.Z.tar.gz"  # Replace with the release version, for example 0.1.0
$parent = "$env:USERPROFILE\Desktop\GitHubRepos\pi-router"
$in = "$parent\$tarFile"

New-Item -ItemType Directory -Force $parent | Out-Null

tar -xzf $in -C $parent
```
