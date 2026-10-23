# Required Contents

- Raspberry Pi 5
- Waveshare PCIe to Gigabit Ethernet Board
- micro SD card
- 2x Ethernet cables

After putting the Pi5 router together, install the latest Raspberry Pi OS Desktop (x64) onto the micro SD card and insert.

DO NOT POWER ON THE PI 5 YET.

Ensure an Ethernet cable is connected from one of your router's LAN ports to the Waveshare PCIe eth0 port. The other ethernet cable should be plugged in from your machine to the Pi 5's built-in ethernet eth0 port.

# Installing Proton-Router

- If using Windows, it is recommended to use the Windows installer packaged in with the latest release.
- A built or downloaded release `*.tar.gz` and `*.sha256.txt` is needed to run the Windows installer.
- To build a `*.tar.gz` and `*.sha256.txt` files, see [Packing / Repack `proton-router-vX.X.X.tar.gz`](#pack--repack-proton-router-vX.X.X.tar.gz) below.

- To build a Windows installer, you may follow the instructions in [BUILD.md](windows-installer\BUILD.md).
  - The installer **_may fail_** on various steps due to either ethernet or ssh disconnects. If this happens, retry the installation. If this still does not work, **re-image the Pi 5 with a fresh Raspberry Pi OS Desktop image** and start again.
- Otherwise, or if using Linux, you may run the setup script steps manually and follow the `proton-router-v*.*.*/docs/setup.md`.
  - A built or downloaded release `*.tar.gz` and `*.sha256.txt` is still needed for the manual setup.

# Final Setup

Once instalation is complete you may follow the final steps for setup:

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

Create a new archive excluding .git:

```powershell
$parent = "$env:USERPROFILE\Desktop\GitHubRepos\proton-router-work"
$release = "proton-router-v0.0.0"
$out = "$parent\$release.tar.gz"

Remove-Item $out -Force -ErrorAction SilentlyContinue

$folderName = $release

tar --exclude="$folderName/.git" -czf $out $folderName
# or if you don't have a .git folder
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
$tarFile="proton-router-v0.0.0.tar.gz"
$parent = "$env:USERPROFILE\Desktop\GitHubRepos\proton-router-work"
$in = "$parent\$tarFile"

New-Item -ItemType Directory -Force $parent | Out-Null

tar -xzf $in -C $parent
```

Do not run git init yet if changes should not be recorded yet in Git history.
When happy with structure, initialize Git:

```sh
git init
git status
```

As long as no remote is added, there is nowhere for Git to push.
