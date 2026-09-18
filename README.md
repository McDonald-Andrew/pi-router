# Getting Started

## Required Contents

-
-
- micro SD card
- 2x Ethernet cables

After putting the Pi5 router together, install the latest Raspberry Pi OS Desktop (x64) onto the micro SD card and insert.

DO NOT POWER ON THE PI 5 YET.

Ensure an Ethernet cable is connected from one of your router's LAN ports to the Waveshare PCIe eth0 port. The other ethernet cable should be plugged in from your machine to the Pi 5's built-in ethernet eth0 port.

## Installing Proton-Router

If on a Windows machine, it is recommended to use the Windows installer packaged in with the latest release. If you wish to build your own tar, see [Packing / Unpacking `proton-router`](#packing--unpacking-proton-router) below. If you wish to build your own installer, you may follow the instructions in [BUILD.md](windows-installer\BUILD.md).

Otherwise, or if on a Linux machine, you may run the setup script steps manually and follow the `proton-router-v*.*.*/docs/setup.md`.

# Packing / Unpacking `proton-router`

## Unpacking the `.tar.gz`

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

## Repack

Create a new archive excluding .git:

```powershell
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
