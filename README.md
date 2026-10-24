# Required Contents

- Raspberry Pi 5
- Waveshare PCIe to Gigabit Ethernet Board
- micro SD card
- 2x Ethernet cables

After putting the Pi5 router together, install the latest Raspberry Pi OS Desktop (x64) onto the micro SD card and insert.

DO NOT POWER ON THE PI 5 YET.

Ensure an Ethernet cable is connected from one of your router's LAN ports to the Waveshare PCIe device's ethernet port (`eth1`). The other ethernet cable should connect your machine to the Pi 5's built-in wthernet port (`eth0`).

# Installing `proton-router`

- If using Windows, it is recommended to use the Windows installer packaged in with the latest release.
- A built or downloaded release `*.tar.gz` and `*.sha256.txt` is needed in the same folder as the `*.exe` Windows installer to run it.
- To build a `*.tar.gz` and `*.sha256.txt` files, see [Pack / Repack `proton-router-vX.X.X.tar.gz](#pack--repack-proton-router-vX.X.X.tar.gz) below.

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
   **The modem MUST be unplugged/reset each time the Pi is rebooted** to ensure proper functionality

6. Turn on the Pi and router.

Congratulations! Your Proton Router is finished!

# Pack / Repack `proton-router-vX.X.X.tar.gz`

Create a new archive:

```powershell
$parent = "$env:USERPROFILE\Desktop\GitHubRepos\proton-router"
$release = "proton-router-vX.Y.Z" # Replace with the release version, for example 0.1.0
$out = "$parent\$release.tar.gz"

Remove-Item $out -Force -ErrorAction SilentlyContinue

$folderName = "src"
tar -czf $out $folderName
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

# Unpack `proton-router-vX.X.X.tar.gz`

```powershell
$tarFile="proton-router-vX.Y.Z"  # Replace with the release version, for example 0.1.0
$parent = "$env:USERPROFILE\Desktop\GitHubRepos\proton-router"
$in = "$parent\$tarFile"

New-Item -ItemType Directory -Force $parent | Out-Null

tar -xzf $in -C $parent
```
